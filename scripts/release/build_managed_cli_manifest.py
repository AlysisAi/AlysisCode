from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

TARGETS = (
    "win32-x64",
    "win32-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64",
    "linux-arm64",
)
SCHEMA_VERSION = 3
NATIVE_EVIDENCE_SCHEMA_VERSION = 2
# Signature domain separator for v3 attestations. Deliberately NOT renamed with
# the rest of the rebrand: this string is hashed into every signature already
# published, so changing it would make every existing signed release fail
# verification. Frozen for the life of schema v3. Must stay byte-identical to
# RELEASE_ATTESTATION_DOMAIN in
# extensions/vscode-alysis/src/runtime/ManagedCliReleaseSecurity.ts.
DOMAIN = "sylliptor-managed-cli-release-v3"
DEFAULT_SOURCE_REPOSITORY = "https://github.com/AlysisAi/AlysisCode"
DEFAULT_PROVENANCE_ISSUER = "https://token.actions.githubusercontent.com"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA1_DIGEST = re.compile(r"^[0-9a-f]{40}$")
APPLE_DEVELOPER_IDENTITY = re.compile(
    r"^Developer ID Application: [^\r\n]{1,384} \([A-Z0-9]{10}\)$"
)
UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
T = TypeVar("T")


@dataclass(frozen=True)
class ArtifactInput:
    target: str
    path: Path
    url: str
    sbom_path: Path | None = None
    native_evidence_path: Path | None = None


def _artifact(value: str) -> ArtifactInput:
    try:
        target, raw_path, url = value.split("=", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("artifact must be TARGET=PATH=HTTPS_URL") from exc
    if target not in TARGETS:
        raise argparse.ArgumentTypeError(f"unsupported target: {target}")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"artifact is not a file: {path}")
    if not url.startswith(f"{DEFAULT_SOURCE_REPOSITORY}/releases/download/"):
        raise argparse.ArgumentTypeError(
            "artifact URL must use the pinned AlysisAi/AlysisCode GitHub release path"
        )
    return ArtifactInput(target=target, path=path, url=url)


def _target_path(value: str) -> tuple[str, Path]:
    try:
        target, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("SBOM must be TARGET=PATH") from exc
    if target not in TARGETS:
        raise argparse.ArgumentTypeError(f"unsupported target: {target}")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"SBOM is not a file: {path}")
    return target, path


def _native_evidence_path(value: str) -> tuple[str, Path]:
    try:
        target, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("native evidence must be TARGET=PATH") from exc
    if target not in TARGETS:
        raise argparse.ArgumentTypeError(f"unsupported target: {target}")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"native evidence is not a file: {path}")
    return target, path


def _validate_https_identity(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ValueError(f"{label} must be a credential-free canonical HTTPS identity")


def _validate_native_signature(target: str, policy: str, signer_identity: str) -> None:
    expected = (
        "authenticode"
        if target.startswith("win32-")
        else "apple-developer-id-notarized"
        if target.startswith("darwin-")
        else "not-applicable"
    )
    if policy != expected:
        raise ValueError(f"{target} native signature policy must be {expected}")
    if (
        not signer_identity
        or len(signer_identity) > 512
        or any(ord(character) < 0x20 for character in signer_identity)
    ):
        raise ValueError(f"{target} native signer identity is invalid")
    if policy == "not-applicable" and signer_identity != "not-applicable":
        raise ValueError("Linux native signer identity must be not-applicable")
    if policy == "authenticode" and (
        not signer_identity.startswith("sha256:")
        or len(signer_identity) != 71
        or any(character not in "0123456789abcdef" for character in signer_identity[7:])
    ):
        raise ValueError("Windows native signer identity must be a SHA-256 certificate digest")
    if (
        policy == "apple-developer-id-notarized"
        and APPLE_DEVELOPER_IDENTITY.fullmatch(signer_identity) is None
    ):
        raise ValueError(
            "macOS native signer identity must be a canonical Developer ID Application identity"
        )


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"native evidence contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_native_evidence(artifact: ArtifactInput, content: bytes) -> tuple[str, str, str]:
    path = artifact.native_evidence_path
    if path is None or not path.is_file():
        raise ValueError(f"{artifact.target} requires an existing native-signature evidence file")
    evidence_bytes = path.read_bytes()
    if not evidence_bytes or len(evidence_bytes) > 64 * 1024:
        raise ValueError(f"{artifact.target} native-signature evidence size is invalid")
    try:
        payload = json.loads(
            evidence_bytes.decode("utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{artifact.target} native-signature evidence is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{artifact.target} native-signature evidence must be a JSON object")

    common_keys = {
        "schemaVersion",
        "target",
        "executable",
        "executableSha256",
        "kind",
        "status",
        "signerIdentity",
    }
    if artifact.target.startswith("win32-"):
        expected_kind = "authenticode"
        policy = "authenticode"
        expected_keys = common_keys | {"signerThumbprint", "timestampSignerIdentity"}
    elif artifact.target.startswith("darwin-"):
        expected_kind = "developer-id-notarization"
        policy = "apple-developer-id-notarized"
        expected_keys = common_keys | {"submissionId"}
    else:
        expected_kind = "linux-hash-and-provenance"
        policy = "not-applicable"
        expected_keys = common_keys
    if set(payload) != expected_keys:
        raise ValueError(f"{artifact.target} native-signature evidence has an invalid schema")
    executable_digest = hashlib.sha256(content).hexdigest()
    if (
        payload.get("schemaVersion") != NATIVE_EVIDENCE_SCHEMA_VERSION
        or payload.get("target") != artifact.target
        or payload.get("executable") != artifact.path.name
        or payload.get("executableSha256") != executable_digest
        or payload.get("kind") != expected_kind
        or payload.get("status") != "verified"
    ):
        raise ValueError(
            f"{artifact.target} native-signature evidence does not bind the verified executable"
        )
    signer_identity = payload.get("signerIdentity")
    if not isinstance(signer_identity, str):
        raise ValueError(f"{artifact.target} native-signature evidence identity is invalid")
    _validate_native_signature(artifact.target, policy, signer_identity)
    if artifact.target.startswith("win32-"):
        signer_thumbprint = payload.get("signerThumbprint")
        timestamp_identity = payload.get("timestampSignerIdentity")
        if (
            not isinstance(signer_thumbprint, str)
            or SHA1_DIGEST.fullmatch(signer_thumbprint) is None
        ):
            raise ValueError(f"{artifact.target} Authenticode thumbprint evidence is invalid")
        if (
            not isinstance(timestamp_identity, str)
            or not timestamp_identity.startswith("sha256:")
            or SHA256_DIGEST.fullmatch(timestamp_identity[7:]) is None
        ):
            raise ValueError(f"{artifact.target} Authenticode timestamp evidence is invalid")
    elif artifact.target.startswith("darwin-"):
        submission_id = payload.get("submissionId")
        if not isinstance(submission_id, str) or UUID.fullmatch(submission_id) is None:
            raise ValueError(f"{artifact.target} notarization submission evidence is invalid")
    return policy, signer_identity, hashlib.sha256(evidence_bytes).hexdigest()


def _attestation(record: dict[str, object]) -> bytes:
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{DOMAIN}\n{canonical}\n".encode()


def _signed_record(
    *,
    release: dict[str, str],
    artifact_version: str,
    cli_version: str,
    compatibility: dict[str, dict[str, str]],
    signing_key_id: str,
    provenance: dict[str, str],
    artifact: dict[str, object],
) -> dict[str, object]:
    return {
        "artifact": artifact,
        "artifactVersion": artifact_version,
        "cliVersion": cli_version,
        "compatibility": compatibility,
        "provenance": provenance,
        "release": release,
        "schemaVersion": SCHEMA_VERSION,
        "signingKeyId": signing_key_id,
    }


def build_manifest(
    *,
    repository_root: Path,
    signing_key_path: Path,
    artifacts: list[ArtifactInput],
    release_tag: str,
    source_repository: str,
    source_commit: str,
    signing_key_id: str,
    provenance_issuer: str,
    provenance_builder_id: str,
) -> dict[str, object]:
    if {artifact.target for artifact in artifacts} != set(TARGETS) or len(artifacts) != len(
        TARGETS
    ):
        raise ValueError("exactly one artifact for each of the six supported targets is required")
    project = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    cli_version = str(project["version"])
    if release_tag != f"v{cli_version}":
        raise ValueError(f"release tag must be v{cli_version}")
    if not SOURCE_COMMIT.fullmatch(source_commit):
        raise ValueError("source commit must be a lowercase 40-character Git commit SHA")
    if not SAFE_IDENTIFIER.fullmatch(signing_key_id):
        raise ValueError("signing key id is invalid")
    _validate_https_identity(source_repository, "source repository")
    _validate_https_identity(provenance_issuer, "provenance issuer")
    _validate_https_identity(provenance_builder_id, "provenance builder id")
    expected_builder_prefix = f"{source_repository}/.github/workflows/"
    expected_builder_suffix = f"@refs/tags/{release_tag}"
    if not provenance_builder_id.startswith(
        expected_builder_prefix
    ) or not provenance_builder_id.endswith(expected_builder_suffix):
        raise ValueError("provenance builder id must bind the source repository and release tag")

    extension_package = json.loads(
        (repository_root / "extensions" / "vscode-alysis" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    extension_version = str(extension_package["version"])
    artifact_version = f"cli-{cli_version}"
    compatibility = {
        "extension": {"min": extension_version, "max": extension_version},
        "protocol": {"min": "1", "max": "1"},
        "cli": {"min": cli_version, "max": cli_version},
    }
    release = {
        "sourceCommit": source_commit,
        "sourceRepository": source_repository,
        "tag": release_tag,
    }
    provenance = {"builderId": provenance_builder_id, "issuer": provenance_issuer}

    private_key = serialization.load_pem_private_key(signing_key_path.read_bytes(), password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or private_key.curve.name not in {
        "secp256r1",
        "prime256v1",
    }:
        raise ValueError("managed CLI signing key must be an unencrypted ECDSA P-256 PEM key")

    manifest_artifacts: list[dict[str, object]] = []
    for artifact in sorted(artifacts, key=lambda item: item.target):
        expected_url = f"{source_repository}/releases/download/{release_tag}/{artifact.path.name}"
        if artifact.url != expected_url:
            raise ValueError(
                f"{artifact.target} URL must bind the source repository, release tag, and filename"
            )
        if artifact.sbom_path is None or not artifact.sbom_path.is_file():
            raise ValueError(f"{artifact.target} requires an existing SBOM file")
        content = artifact.path.read_bytes()
        native_policy, native_identity, native_evidence_digest = _load_native_evidence(
            artifact, content
        )
        unsigned_artifact: dict[str, object] = {
            "executable": artifact.path.name,
            "nativeSignature": {
                "evidenceSha256": native_evidence_digest,
                "policy": native_policy,
                "signerIdentity": native_identity,
            },
            "sbomSha256": hashlib.sha256(artifact.sbom_path.read_bytes()).hexdigest(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "target": artifact.target,
            "url": artifact.url,
        }
        record = _signed_record(
            release=release,
            artifact_version=artifact_version,
            cli_version=cli_version,
            compatibility=compatibility,
            signing_key_id=signing_key_id,
            provenance=provenance,
            artifact=unsigned_artifact,
        )
        signature = private_key.sign(_attestation(record), ec.ECDSA(hashes.SHA256()))
        manifest_artifacts.append(
            {**unsigned_artifact, "signature": base64.b64encode(signature).decode("ascii")}
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "release": release,
        "artifactVersion": artifact_version,
        "cliVersion": cli_version,
        "compatibility": compatibility,
        "signingKeyId": signing_key_id,
        "provenance": provenance,
        "artifacts": manifest_artifacts,
    }


def _unique_map(entries: list[tuple[str, T]], label: str) -> dict[str, T]:
    result: dict[str, T] = {}
    for target, value in entries:
        if target in result:
            raise ValueError(f"duplicate {label} for {target}")
        result[target] = value
    if set(result) != set(TARGETS):
        raise ValueError(f"exactly one {label} for each supported target is required")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign the managed Alysis Code CLI release manifest."
    )
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--signing-key-id", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-repository", default=DEFAULT_SOURCE_REPOSITORY)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--provenance-issuer", default=DEFAULT_PROVENANCE_ISSUER)
    parser.add_argument("--provenance-builder-id", required=True)
    parser.add_argument("--artifact", action="append", type=_artifact, required=True)
    parser.add_argument("--sbom", action="append", type=_target_path, required=True)
    parser.add_argument(
        "--native-evidence", action="append", type=_native_evidence_path, required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sboms = _unique_map(args.sbom, "SBOM")
        native_evidence = _unique_map(args.native_evidence, "native-signature evidence")
        artifacts = [
            replace(
                artifact,
                sbom_path=sboms[artifact.target],
                native_evidence_path=native_evidence[artifact.target],
            )
            for artifact in args.artifact
        ]
        manifest = build_manifest(
            repository_root=args.repository_root.resolve(),
            signing_key_path=args.signing_key.resolve(),
            artifacts=artifacts,
            release_tag=args.release_tag,
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            signing_key_id=args.signing_key_id,
            provenance_issuer=args.provenance_issuer,
            provenance_builder_id=args.provenance_builder_id,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
