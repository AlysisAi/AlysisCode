from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5
from zipfile import BadZipFile, ZipFile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.release.build_managed_cli_manifest import TARGETS, _attestation, _signed_record


class VsixSbomError(ValueError):
    """The candidate VSIX is not bound to its signed managed runtime."""


_FORBIDDEN_ARCHIVE_DIRECTORIES = {
    ".git",
    ".vscode",
    ".vscode-test",
    "design",
    "node_modules",
    "scripts",
    "src",
    "test",
    "tests",
}
_FORBIDDEN_ARCHIVE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "package-lock.json",
}
_FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".jks",
    ".key",
    ".keystore",
    ".map",
    ".p12",
    ".pfx",
    ".pem",
    ".ppk",
    ".vsix",
}
_ALLOWED_PUBLIC_KEY_ENTRY = "extension/resources/managed-cli-release-public.pem"
_MAX_VSIX_ENTRY_BYTES = 512 * 1024 * 1024
_MAX_VSIX_EXPANDED_BYTES = 1024 * 1024 * 1024


def build_vsix_sbom(
    *,
    vsix_path: Path,
    target: str,
    manifest_path: Path,
    public_key_path: Path,
    dependency_sbom_path: Path,
) -> dict[str, Any]:
    if target not in TARGETS:
        raise VsixSbomError("Unsupported VS Code platform target.")
    vsix_bytes = vsix_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    try:
        public_key_bytes = public_key_path.read_bytes()
        public_key = serialization.load_pem_public_key(public_key_bytes)
    except (OSError, ValueError, TypeError) as exc:
        raise VsixSbomError("Managed runtime signing key is unavailable or invalid.") from exc
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or public_key.curve.name != "secp256r1"
    ):
        raise VsixSbomError("Managed runtime signing key must be an ECDSA P-256 public key.")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VsixSbomError("Managed runtime manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 3:
        raise VsixSbomError("Managed runtime manifest schema is unsupported.")

    executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
    manifest_entry = "extension/resources/managed-cli/manifest.json"
    executable_entry = f"extension/resources/managed-cli/{executable}"
    package_entry = "extension/package.json"
    try:
        with ZipFile(vsix_path) as archive:
            _validate_vsix_inventory(archive, executable_entry)
            if archive.read(_ALLOWED_PUBLIC_KEY_ENTRY) != public_key_bytes:
                raise VsixSbomError(
                    "VSIX managed-runtime public key differs from the pinned release key."
                )
            if archive.read(manifest_entry) != manifest_bytes:
                raise VsixSbomError("VSIX managed manifest differs from the signed candidate.")
            executable_bytes = archive.read(executable_entry)
            package = json.loads(archive.read(package_entry))
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VsixSbomError("VSIX is missing a valid package or managed runtime entry.") from exc
    if not isinstance(package, dict):
        raise VsixSbomError("VSIX extension manifest is invalid.")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise VsixSbomError("Managed runtime artifacts are invalid.")
    matches = [
        item for item in artifacts if isinstance(item, dict) and item.get("target") == target
    ]
    if len(matches) != 1:
        raise VsixSbomError(
            "Managed runtime manifest does not contain exactly one target artifact."
        )
    artifact = matches[0]
    executable_digest = _sha256(executable_bytes)
    if artifact.get("executable") != executable or artifact.get("sha256") != executable_digest:
        raise VsixSbomError("VSIX managed runtime bytes do not match the signed manifest.")
    verify_artifact_signature(manifest, artifact, public_key_bytes)

    publisher = _required_text(package, "publisher")
    name = _required_text(package, "name")
    extension_version = _required_text(package, "version")
    cli_version = _required_text(manifest, "cliVersion")
    artifact_url = _required_text(artifact, "url")
    native = artifact.get("nativeSignature")
    if not isinstance(native, dict):
        raise VsixSbomError("Managed runtime native-signature policy is missing.")
    native_policy = _required_text(native, "policy")
    native_identity = _required_text(native, "signerIdentity")
    native_evidence_digest = _required_sha256(native, "evidenceSha256")
    sbom_digest = _required_sha256(artifact, "sbomSha256")
    dependency_sbom = _load_dependency_sbom(dependency_sbom_path, sbom_digest)
    vsix_digest = _sha256(vsix_bytes)
    manifest_digest = _sha256(manifest_bytes)
    extension_ref = f"pkg:vscode/{quote(publisher)}/{quote(name)}@{quote(extension_version)}?target={quote(target)}"
    cli_ref = f"pkg:generic/alysis-code@{quote(cli_version)}?target={quote(target)}"
    managed_component = {
        "type": "application",
        "bom-ref": cli_ref,
        "name": "alysis-code",
        "version": cli_version,
        "purl": cli_ref,
        "hashes": [{"alg": "SHA-256", "content": executable_digest}],
        "externalReferences": [{"type": "distribution", "url": artifact_url}],
        "properties": [
            {"name": "alysis:native-signature-policy", "value": native_policy},
            {"name": "alysis:native-signer-identity", "value": native_identity},
            {"name": "alysis:native-evidence-sha256", "value": native_evidence_digest},
            {"name": "alysis:dependency-sbom-sha256", "value": sbom_digest},
        ],
    }
    components, dependencies = _merge_dependency_sbom(
        dependency_sbom,
        extension_ref=extension_ref,
        cli_ref=cli_ref,
        managed_component=managed_component,
    )
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        # GitHub's attestation parser requires a document ID. Bind it to the
        # exact package while keeping promotion's canonical rebuild deterministic.
        "serialNumber": uuid5(NAMESPACE_URL, f"{extension_ref}#sha256={vsix_digest}").urn,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": extension_ref,
                "publisher": publisher,
                "name": name,
                "version": extension_version,
                "purl": extension_ref,
                "hashes": [{"alg": "SHA-256", "content": vsix_digest}],
                "properties": [
                    {"name": "alysis:platform-target", "value": target},
                    {"name": "alysis:managed-manifest-sha256", "value": manifest_digest},
                ],
            }
        },
        "components": components,
        "dependencies": dependencies,
    }


def _validate_vsix_inventory(archive: ZipFile, executable_entry: str) -> None:
    seen: set[str] = set()
    expanded_bytes = 0
    managed_prefix = "extension/resources/managed-cli/"
    allowed_managed = {
        "extension/resources/managed-cli/manifest.json",
        executable_entry,
    }
    for info in archive.infolist():
        name = info.filename
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or name != normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise VsixSbomError("VSIX contains an unsafe archive path.")
        identity = normalized.casefold()
        if identity in seen:
            raise VsixSbomError("VSIX contains duplicate archive paths.")
        seen.add(identity)
        if info.flag_bits & 0x1:
            raise VsixSbomError("VSIX contains an encrypted entry.")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise VsixSbomError("VSIX contains a symbolic-link entry.")
        if info.file_size > _MAX_VSIX_ENTRY_BYTES:
            raise VsixSbomError("VSIX contains an oversized archive entry.")
        expanded_bytes += info.file_size
        if expanded_bytes > _MAX_VSIX_EXPANDED_BYTES:
            raise VsixSbomError("VSIX expanded size exceeds the release limit.")
        if info.is_dir():
            continue
        if normalized.startswith(managed_prefix) and normalized not in allowed_managed:
            raise VsixSbomError("VSIX contains an unexpected managed-runtime entry.")
        if not normalized.startswith("extension/"):
            continue
        extension_parts = [part.casefold() for part in parts[1:]]
        filename = extension_parts[-1]
        if any(part in _FORBIDDEN_ARCHIVE_DIRECTORIES for part in extension_parts[:-1]):
            raise VsixSbomError("VSIX contains a development-only directory.")
        if filename in _FORBIDDEN_ARCHIVE_NAMES:
            raise VsixSbomError("VSIX contains a forbidden credential or build file.")
        if filename.startswith(".env") and filename not in {
            ".env.defaults",
            ".env.dist",
            ".env.example",
            ".env.sample",
            ".env.template",
        }:
            raise VsixSbomError("VSIX contains a forbidden environment file.")
        suffix = Path(filename).suffix
        if suffix in _FORBIDDEN_ARCHIVE_SUFFIXES and normalized != _ALLOWED_PUBLIC_KEY_ENTRY:
            raise VsixSbomError("VSIX contains a forbidden secret, debug, or nested-package file.")
    if archive.testzip() is not None:
        raise VsixSbomError("VSIX contains a corrupt archive entry.")


def _load_dependency_sbom(path: Path, expected_digest: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise VsixSbomError("Managed runtime dependency SBOM is unavailable.") from exc
    if _sha256(raw) != expected_digest:
        raise VsixSbomError("Managed runtime dependency SBOM does not match the signed manifest.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VsixSbomError("Managed runtime dependency SBOM is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise VsixSbomError("Managed runtime dependency SBOM must be a JSON object.")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise VsixSbomError("Managed runtime dependency SBOM must be CycloneDX 1.5.")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise VsixSbomError("Managed runtime dependency SBOM version is invalid.")
    if payload.get("services") not in (None, []):
        raise VsixSbomError("Managed runtime dependency SBOM services cannot be merged safely.")
    return payload


def _merge_dependency_sbom(
    source: dict[str, Any],
    *,
    extension_ref: str,
    cli_ref: str,
    managed_component: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metadata = source.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(root, dict):
        raise VsixSbomError("Managed runtime dependency SBOM root component is missing.")
    root_ref = _component_ref(root, context="root component")
    if root_ref == extension_ref:
        raise VsixSbomError("Dependency SBOM root ref collides with the VSIX component.")

    raw_components = source.get("components", [])
    if not isinstance(raw_components, list):
        raise VsixSbomError("Managed runtime dependency SBOM components are invalid.")
    by_source_ref: dict[str, dict[str, Any]] = {}
    for value in raw_components:
        if not isinstance(value, dict):
            raise VsixSbomError("Managed runtime dependency SBOM contains an invalid component.")
        component = _validated_component(value)
        ref = str(component["bom-ref"])
        prior = by_source_ref.get(ref)
        if prior is not None and _canonical(prior) != _canonical(component):
            raise VsixSbomError("Managed runtime dependency SBOM has conflicting component refs.")
        by_source_ref[ref] = component

    aliases = {root_ref: cli_ref}
    retained: dict[str, dict[str, Any]] = {}
    purl_refs: dict[str, str] = {}
    for ref, component in sorted(by_source_ref.items()):
        if ref == root_ref:
            continue
        if ref in {extension_ref, cli_ref}:
            raise VsixSbomError("Dependency component ref collides with the final VSIX SBOM.")
        purl = component.get("purl")
        if isinstance(purl, str) and purl:
            if purl in {extension_ref, cli_ref}:
                raise VsixSbomError(
                    "Dependency component identity collides with a packaged application."
                )
            duplicate_ref = purl_refs.get(purl)
            if duplicate_ref is not None:
                prior = retained[duplicate_ref]
                if _canonical(_without_ref(prior)) != _canonical(_without_ref(component)):
                    raise VsixSbomError(
                        "Managed runtime dependency SBOM has conflicting duplicate packages."
                    )
                aliases[ref] = duplicate_ref
                continue
            purl_refs[purl] = ref
        retained[ref] = component

    source_refs = {root_ref, *by_source_ref}
    raw_dependencies = source.get("dependencies", [])
    if not isinstance(raw_dependencies, list):
        raise VsixSbomError("Managed runtime dependency graph is invalid.")
    graph: dict[str, set[str]] = {}
    for value in raw_dependencies:
        if not isinstance(value, dict):
            raise VsixSbomError("Managed runtime dependency graph contains an invalid record.")
        unsupported = set(value) - {"ref", "dependsOn"}
        if unsupported:
            raise VsixSbomError("Managed runtime dependency graph uses unsupported fields.")
        source_ref = _dependency_ref(value.get("ref"), source_refs)
        target_ref = aliases.get(source_ref, source_ref)
        record = graph.setdefault(target_ref, set())
        raw_refs = value.get("dependsOn", [])
        if not isinstance(raw_refs, list):
            raise VsixSbomError("Managed runtime dependency graph references are invalid.")
        for raw_ref in raw_refs:
            dependency_ref = _dependency_ref(raw_ref, source_refs)
            mapped_ref = aliases.get(dependency_ref, dependency_ref)
            if mapped_ref != target_ref:
                record.add(mapped_ref)

    final_refs = {cli_ref, *retained}
    for ref, dependencies in graph.items():
        if ref not in final_refs or not dependencies <= final_refs:
            raise VsixSbomError("Managed runtime dependency graph contains a dangling reference.")
    for ref in final_refs:
        graph.setdefault(ref, set())

    _attach_unreachable_components(graph, cli_ref=cli_ref, component_refs=set(retained))
    graph[extension_ref] = {cli_ref}
    components = [managed_component, *retained.values()]
    components.sort(key=lambda item: str(item["bom-ref"]))
    dependencies = [_dependency_record(ref, graph[ref]) for ref in sorted(graph)]
    return components, dependencies


def _attach_unreachable_components(
    graph: dict[str, set[str]],
    *,
    cli_ref: str,
    component_refs: set[str],
) -> None:
    reachable: set[str] = set()
    pending = [cli_ref]
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        reachable.add(ref)
        pending.extend(graph[ref])
    unreachable = component_refs - reachable
    if not unreachable:
        return
    depended_on = {
        dependency for ref in unreachable for dependency in graph[ref] if dependency in unreachable
    }
    roots = unreachable - depended_on
    graph[cli_ref].update(roots or unreachable)


def _validated_component(value: dict[str, Any]) -> dict[str, Any]:
    component = dict(value)
    _component_ref(component, context="component")
    if not isinstance(component.get("name"), str) or not str(component["name"]).strip():
        raise VsixSbomError("Managed runtime dependency component name is invalid.")
    valid_types = {
        "application",
        "container",
        "data",
        "device",
        "device-driver",
        "file",
        "firmware",
        "framework",
        "library",
        "machine-learning-model",
        "operating-system",
        "platform",
    }
    if component.get("type") not in valid_types:
        raise VsixSbomError("Managed runtime dependency component type is invalid.")
    for key in ("version", "purl"):
        if key in component and not isinstance(component[key], str):
            raise VsixSbomError(f"Managed runtime dependency component {key} is invalid.")
    return component


def _component_ref(component: dict[str, Any], *, context: str) -> str:
    value = component.get("bom-ref")
    if not isinstance(value, str) or not value.strip():
        raise VsixSbomError(f"Managed runtime dependency SBOM {context} ref is invalid.")
    return value


def _dependency_ref(value: Any, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise VsixSbomError("Managed runtime dependency graph contains a dangling reference.")
    return value


def _dependency_record(ref: str, value: set[str]) -> dict[str, Any]:
    record: dict[str, Any] = {"ref": ref}
    if value:
        record["dependsOn"] = sorted(value)
    return record


def _without_ref(component: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in component.items() if key != "bom-ref"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify_artifact_signature(
    manifest: dict[str, Any], artifact: dict[str, Any], public_key_bytes: bytes
) -> None:
    """Verify one managed-runtime record using the exact packaged public key bytes."""

    signature_text = artifact.get("signature")
    if not isinstance(signature_text, str):
        raise VsixSbomError("Managed runtime artifact signature is missing.")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public_key = serialization.load_pem_public_key(public_key_bytes)
    except (ValueError, TypeError) as exc:
        raise VsixSbomError("Managed runtime signing material is invalid.") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise VsixSbomError("Managed runtime signing key is not ECDSA.")
    unsigned_artifact = {key: value for key, value in artifact.items() if key != "signature"}
    try:
        record = _signed_record(
            release=manifest["release"],
            artifact_version=_required_text(manifest, "artifactVersion"),
            cli_version=_required_text(manifest, "cliVersion"),
            compatibility=manifest["compatibility"],
            signing_key_id=_required_text(manifest, "signingKeyId"),
            provenance=manifest["provenance"],
            artifact=unsigned_artifact,
        )
        public_key.verify(signature, _attestation(record), ec.ECDSA(hashes.SHA256()))
    except (KeyError, TypeError, ValueError) as exc:
        raise VsixSbomError("Managed runtime signed record is invalid.") from exc
    except Exception as exc:  # cryptography intentionally exposes several backend exceptions
        raise VsixSbomError("Managed runtime signature verification failed.") from exc


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VsixSbomError(f"Required field {key} is missing.")
    return value.strip()


def _required_sha256(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise VsixSbomError(f"Required field {key} is not a SHA-256 digest.")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a signed-runtime-bound VSIX CycloneDX SBOM."
    )
    parser.add_argument("--vsix", type=Path, required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--dependency-sbom", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _atomic_json(
        args.output,
        build_vsix_sbom(
            vsix_path=args.vsix,
            target=args.target,
            manifest_path=args.manifest,
            public_key_path=args.public_key,
            dependency_sbom_path=args.dependency_sbom,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
