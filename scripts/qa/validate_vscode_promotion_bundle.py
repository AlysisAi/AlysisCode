#!/usr/bin/env python3
"""Validate the complete six-target VS Code Marketplace promotion bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.validate_vscode_environment_acceptance import (  # noqa: E402
    REPORT_FILENAME as ENVIRONMENT_REPORT_FILENAME,
)
from scripts.qa.validate_vscode_environment_acceptance import (  # noqa: E402
    EnvironmentAcceptanceError,
)
from scripts.qa.validate_vscode_environment_acceptance import (  # noqa: E402
    validate_file as validate_environment_acceptance_file,
)
from scripts.qa.validate_vscode_human_evidence_bundle import (  # noqa: E402
    BUNDLE_FILENAME as HUMAN_EVIDENCE_BUNDLE_FILENAME,
)
from scripts.qa.validate_vscode_human_evidence_bundle import (  # noqa: E402
    HumanEvidenceBundleError,
)
from scripts.qa.validate_vscode_human_evidence_bundle import (  # noqa: E402
    validate_bundle as validate_human_evidence_bundle,
)
from scripts.qa.validate_vscode_installed_live_provider import (  # noqa: E402
    InstalledLiveProviderEvidenceError,
    validate_installed_live_provider_evidence,
)
from scripts.qa.validate_vscode_manual_provider_report import (  # noqa: E402
    ManualReportValidationError,
    validate_candidate_binding,
    validate_report,
)
from scripts.qa.validate_vscode_production_signoff import (  # noqa: E402
    REPORT_FILENAME as PRODUCTION_SIGNOFF_FILENAME,
)
from scripts.qa.validate_vscode_production_signoff import (  # noqa: E402
    ProductionSignoffError,
)
from scripts.qa.validate_vscode_production_signoff import (  # noqa: E402
    validate_file as validate_production_signoff_file,
)
from scripts.qa.validate_vscode_remote_acceptance import (  # noqa: E402
    RemoteAcceptanceError,
)
from scripts.qa.validate_vscode_remote_acceptance import (  # noqa: E402
    validate_evidence as validate_remote_acceptance_evidence,
)
from scripts.qa.validate_vscode_untrusted_workspace_report import (  # noqa: E402
    UntrustedWorkspaceReportError,
)
from scripts.qa.validate_vscode_untrusted_workspace_report import (  # noqa: E402
    validate_candidate as validate_untrusted_workspace_candidate,
)
from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    SUPPORTED_PLATFORM_TARGETS,
    DogfoodError,
    validate_production_dogfood_candidate_binding,
)
from scripts.release.build_vscode_vsix_sbom import (  # noqa: E402
    VsixSbomError,
    build_vsix_sbom,
    verify_artifact_signature,
)


class PromotionBundleValidationError(RuntimeError):
    """Promotion evidence is incomplete or not bound to the candidate bytes."""


RECEIPT_SCHEMA_NAME = "vscode-promotion-validation-receipt"
RECEIPT_SCHEMA_VERSION = 7
GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def validate_promotion_bundle(
    candidate_dir: Path,
    evidence_dir: Path,
    managed_runtime_dir: Path,
    installed_live_provider_dir: Path,
    untrusted_workspace_dir: Path,
    remote_acceptance_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    candidate_run_attempt: int,
    installed_live_provider_run_id: int,
    installed_live_provider_run_attempt: int,
    untrusted_workspace_run_id: int,
    untrusted_workspace_run_attempt: int,
    remote_acceptance_run_id: int,
    remote_acceptance_wsl_run_attempt: int,
    remote_acceptance_remote_ssh_run_attempt: int,
    trusted_public_key_path: Path | None = None,
    channel: str = "stable",
) -> tuple[Path, ...]:
    if candidate_run_attempt != 1:
        raise PromotionBundleValidationError(
            "Candidate artifacts must come from a first-attempt workflow run."
        )
    targets = tuple(sorted(SUPPORTED_PLATFORM_TARGETS))
    expected_candidates = {
        *(f"vscode-alysis-{target}.vsix" for target in targets),
        *(f"vscode-alysis-{target}.cdx.json" for target in targets),
        *(f"production-dogfood-{target}.json" for target in targets),
    }
    expected_evidence = {
        *(f"vscode-alysis-{target}.manual-provider.json" for target in targets),
        ENVIRONMENT_REPORT_FILENAME,
        HUMAN_EVIDENCE_BUNDLE_FILENAME,
        PRODUCTION_SIGNOFF_FILENAME,
    }
    expected_installed_live_provider = {
        f"vscode-alysis-{target}.installed-live-provider.json" for target in targets
    }
    expected_untrusted_workspace = {"vscode-alysis-linux-x64.untrusted-workspace.json"}
    expected_remote_acceptance = {
        "vscode-remote-acceptance-wsl.json",
        "vscode-remote-acceptance-remote_ssh.json",
    }
    _require_exact_inventory(candidate_dir, expected_candidates, label="candidate")
    _require_exact_inventory(evidence_dir, expected_evidence, label="manual evidence")
    _require_exact_inventory(
        installed_live_provider_dir,
        expected_installed_live_provider,
        label="installed live-provider evidence",
    )
    _require_exact_inventory(
        untrusted_workspace_dir,
        expected_untrusted_workspace,
        label="untrusted-workspace evidence",
    )
    _require_exact_inventory(
        remote_acceptance_dir,
        expected_remote_acceptance,
        label="remote acceptance evidence",
    )
    _validate_managed_runtime_release(
        managed_runtime_dir,
        candidate_dir,
        release_tag=release_tag,
        source_sha=source_sha,
        trusted_public_key_path=trusted_public_key_path,
    )

    packages: list[Path] = []
    for target in targets:
        package = candidate_dir / f"vscode-alysis-{target}.vsix"
        validate_target_bundle(
            channel=channel,
            target=target,
            candidate_vsix=package,
            sbom_path=candidate_dir / f"vscode-alysis-{target}.cdx.json",
            dogfood_path=candidate_dir / f"production-dogfood-{target}.json",
            report_path=evidence_dir / f"vscode-alysis-{target}.manual-provider.json",
            release_tag=release_tag,
            source_sha=source_sha,
            candidate_run_id=candidate_run_id,
            trusted_public_key_path=trusted_public_key_path,
        )
        try:
            installed_report = _json_object(
                installed_live_provider_dir
                / f"vscode-alysis-{target}.installed-live-provider.json",
                "installed live-provider report",
            )
            validate_installed_live_provider_evidence(
                installed_report,
                channel=channel,
                candidate_vsix=package,
                production_dogfood=candidate_dir / f"production-dogfood-{target}.json",
                expected_target=target,
                expected_release_tag=release_tag,
                expected_source_sha=source_sha,
                expected_candidate_run_id=str(candidate_run_id),
                expected_workflow_run_id=installed_live_provider_run_id,
                expected_workflow_run_attempt=installed_live_provider_run_attempt,
                provider_origin_policy=(
                    PROJECT_ROOT
                    / ".github"
                    / "release-policy"
                    / "vscode-live-provider-origins.json"
                ),
            )
        except (InstalledLiveProviderEvidenceError, OSError) as exc:
            raise PromotionBundleValidationError(str(exc)) from exc
        packages.append(package)
    try:
        validate_environment_acceptance_file(
            evidence_dir / ENVIRONMENT_REPORT_FILENAME,
            candidate_dir,
            expected_release_tag=release_tag,
            expected_source_sha=source_sha,
            expected_candidate_run_id=candidate_run_id,
        )
    except EnvironmentAcceptanceError as exc:
        raise PromotionBundleValidationError(str(exc)) from exc
    try:
        validate_human_evidence_bundle(
            evidence_dir / HUMAN_EVIDENCE_BUNDLE_FILENAME,
            evidence_dir,
        )
    except HumanEvidenceBundleError as exc:
        raise PromotionBundleValidationError(str(exc)) from exc
    try:
        validate_production_signoff_file(
            evidence_dir / PRODUCTION_SIGNOFF_FILENAME,
            candidate_dir,
            channel=channel,
            expected_release_tag=release_tag,
            expected_source_sha=source_sha,
            expected_candidate_run_id=candidate_run_id,
        )
    except ProductionSignoffError as exc:
        raise PromotionBundleValidationError(str(exc)) from exc
    _validate_installed_workspace_acceptance(
        candidate_dir,
        untrusted_workspace_dir,
        remote_acceptance_dir,
        channel=channel,
        release_tag=release_tag,
        source_sha=source_sha,
        candidate_run_id=candidate_run_id,
        untrusted_workspace_run_id=untrusted_workspace_run_id,
        untrusted_workspace_run_attempt=untrusted_workspace_run_attempt,
        remote_acceptance_run_id=remote_acceptance_run_id,
        remote_acceptance_wsl_run_attempt=remote_acceptance_wsl_run_attempt,
        remote_acceptance_remote_ssh_run_attempt=remote_acceptance_remote_ssh_run_attempt,
        trusted_public_key_path=trusted_public_key_path,
    )
    return tuple(packages)


def _validate_installed_workspace_acceptance(
    candidate_dir: Path,
    untrusted_workspace_dir: Path,
    remote_acceptance_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    untrusted_workspace_run_id: int,
    untrusted_workspace_run_attempt: int,
    remote_acceptance_run_id: int,
    remote_acceptance_wsl_run_attempt: int,
    remote_acceptance_remote_ssh_run_attempt: int,
    trusted_public_key_path: Path | None,
    channel: str = "stable",
) -> None:
    if (
        untrusted_workspace_run_id <= 0
        or untrusted_workspace_run_attempt <= 0
        or remote_acceptance_run_id <= 0
        or remote_acceptance_wsl_run_attempt <= 0
        or remote_acceptance_remote_ssh_run_attempt <= 0
    ):
        raise PromotionBundleValidationError("Acceptance workflow identities must be positive.")
    untrusted_report = _json_object(
        untrusted_workspace_dir / "vscode-alysis-linux-x64.untrusted-workspace.json",
        "untrusted-workspace report",
    )
    try:
        validate_untrusted_workspace_candidate(
            untrusted_report,
            candidate_dir / "vscode-alysis-linux-x64.vsix",
            channel=channel,
            trusted_public_key_path=trusted_public_key_path,
        )
    except UntrustedWorkspaceReportError as exc:
        raise PromotionBundleValidationError(str(exc)) from exc
    expected_untrusted_identity = {
        "release_tag": release_tag,
        "source_sha": source_sha,
        "candidate_run_id": candidate_run_id,
        "workflow_run_id": untrusted_workspace_run_id,
        "workflow_run_attempt": untrusted_workspace_run_attempt,
        "platform_target": "linux-x64",
    }
    if any(
        untrusted_report.get(field) != expected
        for field, expected in expected_untrusted_identity.items()
    ):
        raise PromotionBundleValidationError(
            "Untrusted-workspace evidence does not bind the exact release candidate."
        )

    expected_attempts = {
        "wsl": remote_acceptance_wsl_run_attempt,
        "remote_ssh": remote_acceptance_remote_ssh_run_attempt,
    }
    for environment, expected_attempt in expected_attempts.items():
        report = _json_object(
            remote_acceptance_dir / f"vscode-remote-acceptance-{environment}.json",
            f"{environment} acceptance report",
        )
        vscode = report.get("vscode")
        host = report.get("host")
        if not isinstance(vscode, dict) or not isinstance(host, dict):
            raise PromotionBundleValidationError(
                f"{environment} acceptance identity is incomplete."
            )
        try:
            validate_remote_acceptance_evidence(
                report,
                candidate_dir,
                expected_environment=environment,
                expected_release_tag=release_tag,
                expected_source_sha=source_sha,
                expected_candidate_run_id=candidate_run_id,
                expected_workflow_run_id=remote_acceptance_run_id,
                expected_workflow_run_attempt=expected_attempt,
                expected_vscode_version="1.90.0",
                expected_vscode_commit=vscode.get("commit"),
                expected_authority_sha256=host.get("authority_sha256"),
                expected_hostname_sha256=host.get("hostname_sha256"),
                expected_workspace_path_sha256=host.get("workspace_path_sha256"),
            )
        except RemoteAcceptanceError as exc:
            raise PromotionBundleValidationError(str(exc)) from exc


def _validate_managed_runtime_release(
    managed_runtime_dir: Path,
    candidate_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    trusted_public_key_path: Path | None,
) -> None:
    targets = tuple(sorted(SUPPORTED_PLATFORM_TARGETS))
    executables = {
        target: f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        for target in targets
    }
    expected = {
        "manifest.json",
        *(executables.values()),
        *(f"{target}.cdx.json" for target in targets),
        *(f"{target}.native-signature.json" for target in targets),
    }
    _require_exact_inventory(managed_runtime_dir, expected, label="managed runtime")
    if re.fullmatch(r"v\d+\.\d+\.\d+", release_tag) is None:
        raise PromotionBundleValidationError("Release tag must use vX.Y.Z format.")
    if GIT_OBJECT_ID_RE.fullmatch(source_sha) is None:
        raise PromotionBundleValidationError("Source SHA must be a full lowercase Git object ID.")

    manifest_path = managed_runtime_dir / "manifest.json"
    manifest = _json_object(manifest_path, "managed runtime manifest")
    if manifest.get("schemaVersion") != 3:
        raise PromotionBundleValidationError("Managed runtime manifest schema is unsupported.")
    release = manifest.get("release")
    if not isinstance(release, dict) or release != {
        "sourceCommit": source_sha,
        "sourceRepository": "https://github.com/AlysisAi/AlysisCode",
        "tag": release_tag,
    }:
        raise PromotionBundleValidationError(
            "Managed runtime manifest does not bind the exact release source."
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise PromotionBundleValidationError("Managed runtime manifest artifacts are invalid.")
    by_target = {
        artifact.get("target"): artifact for artifact in artifacts if isinstance(artifact, dict)
    }
    if len(by_target) != len(artifacts) or set(by_target) != set(targets):
        raise PromotionBundleValidationError(
            "Managed runtime manifest must contain exactly the six supported targets."
        )

    trusted_key = trusted_public_key_path or (
        PROJECT_ROOT
        / "extensions"
        / "vscode-alysis"
        / "resources"
        / "managed-cli-release-public.pem"
    )
    try:
        public_key_bytes = trusted_key.read_bytes()
    except OSError as exc:
        raise PromotionBundleValidationError(
            "Pinned managed-runtime release public key is unavailable."
        ) from exc

    for target in targets:
        artifact = by_target[target]
        executable_name = executables[target]
        executable_path = managed_runtime_dir / executable_name
        executable_bytes = executable_path.read_bytes()
        dependency_sbom_path = managed_runtime_dir / f"{target}.cdx.json"
        expected_url = (
            f"https://github.com/AlysisAi/AlysisCode/releases/download/"
            f"{release_tag}/{executable_name}"
        )
        if (
            artifact.get("executable") != executable_name
            or artifact.get("url") != expected_url
            or artifact.get("sha256") != hashlib.sha256(executable_bytes).hexdigest()
            or artifact.get("size") != len(executable_bytes)
            or artifact.get("sbomSha256")
            != hashlib.sha256(dependency_sbom_path.read_bytes()).hexdigest()
        ):
            raise PromotionBundleValidationError(
                f"Managed runtime release bytes do not match the signed {target} record."
            )
        try:
            verify_artifact_signature(manifest, artifact, public_key_bytes)
        except VsixSbomError as exc:
            raise PromotionBundleValidationError(
                f"Managed runtime signature is invalid for {target}: {exc}"
            ) from exc

        native = artifact.get("nativeSignature")
        if not isinstance(native, dict):
            raise PromotionBundleValidationError(
                f"Managed runtime native signature policy is missing for {target}."
            )
        native_evidence_path = managed_runtime_dir / f"{target}.native-signature.json"
        native_record = _json_object(native_evidence_path, "native signature evidence")
        expected_kind = (
            "authenticode"
            if target.startswith("win32-")
            else "developer-id-notarization"
            if target.startswith("darwin-")
            else "linux-hash-and-provenance"
        )
        expected_native_record: dict[str, object] = {
            "schemaVersion": 2,
            "target": target,
            "executable": executable_name,
            "executableSha256": hashlib.sha256(executable_bytes).hexdigest(),
            "kind": expected_kind,
            "status": "verified",
            "signerIdentity": native.get("signerIdentity"),
        }
        if target.startswith("win32-"):
            expected_native_record.update(
                signerThumbprint=native_record.get("signerThumbprint"),
                timestampSignerIdentity=native_record.get("timestampSignerIdentity"),
            )
            if (
                re.fullmatch(r"[0-9a-f]{40}", str(native_record.get("signerThumbprint"))) is None
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(native_record.get("timestampSignerIdentity")),
                )
                is None
            ):
                raise PromotionBundleValidationError(
                    f"Native Authenticode evidence is invalid for {target}."
                )
        elif target.startswith("darwin-"):
            expected_native_record["submissionId"] = native_record.get("submissionId")
            if (
                re.fullmatch(
                    r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
                    str(native_record.get("submissionId")),
                )
                is None
            ):
                raise PromotionBundleValidationError(
                    f"Native notarization evidence is invalid for {target}."
                )
        if (
            native_record != expected_native_record
            or native.get("evidenceSha256")
            != hashlib.sha256(native_evidence_path.read_bytes()).hexdigest()
        ):
            raise PromotionBundleValidationError(
                f"Native signature evidence is inconsistent for {target}."
            )

        try:
            expected_sbom = build_vsix_sbom(
                vsix_path=candidate_dir / f"vscode-alysis-{target}.vsix",
                target=target,
                manifest_path=manifest_path,
                public_key_path=trusted_key,
                dependency_sbom_path=dependency_sbom_path,
            )
        except VsixSbomError as exc:
            raise PromotionBundleValidationError(
                f"Candidate VSIX/runtime/SBOM binding is invalid for {target}: {exc}"
            ) from exc
        observed_sbom = _json_object(
            candidate_dir / f"vscode-alysis-{target}.cdx.json",
            "VSIX SBOM",
        )
        if observed_sbom != expected_sbom:
            raise PromotionBundleValidationError(
                f"VSIX SBOM is not the canonical package-bound document for {target}."
            )


def validate_target_bundle(
    *,
    target: str,
    candidate_vsix: Path,
    sbom_path: Path,
    dogfood_path: Path,
    report_path: Path,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    trusted_public_key_path: Path | None = None,
    channel: str = "stable",
) -> None:
    if target not in SUPPORTED_PLATFORM_TARGETS:
        raise PromotionBundleValidationError(f"Unsupported VS Code platform target: {target}")
    digest = hashlib.sha256(candidate_vsix.read_bytes()).hexdigest()
    try:
        report = _json_object(report_path, "manual provider report")
        validate_report(report)
        validate_candidate_binding(
            report,
            candidate_vsix,
            trusted_public_key_path=trusted_public_key_path,
            channel=channel,
        )
        dogfood = _json_object(dogfood_path, "production dogfood summary")
        validate_production_dogfood_candidate_binding(
            dogfood,
            candidate_vsix,
            channel=channel,
            trusted_public_key_path=trusted_public_key_path,
            require_vscode_compatibility=True,
        )
    except (ManualReportValidationError, DogfoodError, OSError) as exc:
        raise PromotionBundleValidationError(str(exc)) from exc

    if report.get("platform_target") != target or dogfood.get("platform_target") != target:
        raise PromotionBundleValidationError("Promotion evidence targets do not match the package.")
    if (
        report.get("release_tag") != release_tag
        or dogfood.get("release_tag") != release_tag
        or report.get("source_sha") != source_sha
        or dogfood.get("source_sha") != source_sha
        or report.get("candidate_run_id") != candidate_run_id
    ):
        raise PromotionBundleValidationError(
            "Manual and automated evidence must bind the exact candidate run and release source."
        )
    if report.get("vsix_sha256") != digest or dogfood.get("vsix_sha256") != digest:
        raise PromotionBundleValidationError(
            "Manual and automated evidence must bind the exact VSIX SHA-256."
        )
    if Path(str(dogfood.get("vsix") or "").replace("\\", "/")).name != candidate_vsix.name:
        raise PromotionBundleValidationError("Production dogfood names a different VSIX package.")
    production_dogfood_digest = hashlib.sha256(dogfood_path.read_bytes()).hexdigest()
    if report.get("production_dogfood_sha256") != production_dogfood_digest:
        raise PromotionBundleValidationError(
            "Manual evidence does not bind the exact production dogfood report bytes."
        )
    for field in (
        "source_repository",
        "extension_version",
        "managed_artifact_version",
        "managed_cli_sha256",
        "vscode_version",
        "host_platform",
        "host_arch",
        "remote_name",
        "workspace_trusted",
        "workspace_scheme",
        "workspace_authority",
    ):
        if report.get(field) != dogfood.get(field):
            raise PromotionBundleValidationError(
                f"Manual and automated evidence disagree on {field}."
            )
    _validate_sbom(sbom_path, target=target, candidate_digest=digest)


def _require_exact_inventory(directory: Path, expected: set[str], *, label: str) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise PromotionBundleValidationError(f"{label.title()} directory is unavailable.") from exc
    if any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise PromotionBundleValidationError(f"{label.title()} inventory must contain files only.")
    observed = {entry.name for entry in entries}
    if len(entries) != len(observed) or observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        raise PromotionBundleValidationError(
            f"{label.title()} inventory mismatch; missing={missing}, unexpected={unexpected}."
        )


def build_validation_receipt(
    candidate_dir: Path,
    evidence_dir: Path,
    managed_runtime_dir: Path,
    installed_live_provider_dir: Path,
    untrusted_workspace_dir: Path,
    remote_acceptance_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    candidate_run_attempt: int,
    installed_live_provider_run_id: int,
    installed_live_provider_run_attempt: int,
    untrusted_workspace_run_id: int,
    untrusted_workspace_run_attempt: int,
    remote_acceptance_run_id: int,
    remote_acceptance_wsl_run_attempt: int,
    remote_acceptance_remote_ssh_run_attempt: int,
    evidence_run_id: int,
    evidence_run_attempt: int,
    promotion_run_id: int | None = None,
    promotion_run_attempt: int | None = None,
    channel: str = "stable",
) -> dict[str, Any]:
    if channel not in {"stable", "beta"}:
        raise PromotionBundleValidationError("Unsupported release channel.")
    if candidate_run_attempt != 1:
        raise PromotionBundleValidationError(
            "Candidate artifacts must come from a first-attempt workflow run."
        )
    if any(
        value <= 0
        for value in (
            candidate_run_id,
            installed_live_provider_run_id,
            installed_live_provider_run_attempt,
            untrusted_workspace_run_id,
            untrusted_workspace_run_attempt,
            remote_acceptance_run_id,
            remote_acceptance_wsl_run_attempt,
            remote_acceptance_remote_ssh_run_attempt,
            evidence_run_id,
            evidence_run_attempt,
        )
    ):
        raise PromotionBundleValidationError("Workflow run IDs must be positive.")
    if (promotion_run_id is None) != (promotion_run_attempt is None):
        raise PromotionBundleValidationError(
            "Promotion workflow run ID and attempt must be supplied together."
        )
    if promotion_run_id is not None and (
        promotion_run_id <= 0 or promotion_run_attempt is None or promotion_run_attempt <= 0
    ):
        raise PromotionBundleValidationError("Promotion workflow identity must be positive.")
    return {
        "schema_name": RECEIPT_SCHEMA_NAME,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "channel": channel,
        "release_tag": release_tag,
        "source_sha": source_sha,
        "candidate_run_id": candidate_run_id,
        "candidate_run_attempt": candidate_run_attempt,
        "installed_live_provider_run_id": installed_live_provider_run_id,
        "installed_live_provider_run_attempt": installed_live_provider_run_attempt,
        "untrusted_workspace_run_id": untrusted_workspace_run_id,
        "untrusted_workspace_run_attempt": untrusted_workspace_run_attempt,
        "remote_acceptance_run_id": remote_acceptance_run_id,
        "remote_acceptance_wsl_run_attempt": remote_acceptance_wsl_run_attempt,
        "remote_acceptance_remote_ssh_run_attempt": remote_acceptance_remote_ssh_run_attempt,
        "evidence_run_id": evidence_run_id,
        "evidence_run_attempt": evidence_run_attempt,
        "promotion_run_id": promotion_run_id,
        "promotion_run_attempt": promotion_run_attempt,
        "inventories": {
            "candidates": _inventory_hashes(candidate_dir),
            "evidence": _inventory_hashes(evidence_dir),
            "installed_live_provider": _inventory_hashes(installed_live_provider_dir),
            "untrusted_workspace": _inventory_hashes(untrusted_workspace_dir),
            "remote_acceptance": _inventory_hashes(remote_acceptance_dir),
            "managed_runtime": _inventory_hashes(managed_runtime_dir),
        },
    }


def verify_validation_receipt(
    receipt_path: Path,
    candidate_dir: Path,
    evidence_dir: Path,
    managed_runtime_dir: Path,
    installed_live_provider_dir: Path,
    untrusted_workspace_dir: Path,
    remote_acceptance_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    candidate_run_attempt: int,
    installed_live_provider_run_id: int,
    installed_live_provider_run_attempt: int,
    untrusted_workspace_run_id: int,
    untrusted_workspace_run_attempt: int,
    remote_acceptance_run_id: int,
    remote_acceptance_wsl_run_attempt: int,
    remote_acceptance_remote_ssh_run_attempt: int,
    evidence_run_id: int,
    evidence_run_attempt: int,
    promotion_run_id: int | None = None,
    promotion_run_attempt: int | None = None,
    channel: str = "stable",
) -> None:
    observed = _json_object(receipt_path, "promotion validation receipt")
    expected = build_validation_receipt(
        candidate_dir,
        evidence_dir,
        managed_runtime_dir,
        installed_live_provider_dir,
        untrusted_workspace_dir,
        remote_acceptance_dir,
        channel=channel,
        release_tag=release_tag,
        source_sha=source_sha,
        candidate_run_id=candidate_run_id,
        candidate_run_attempt=candidate_run_attempt,
        installed_live_provider_run_id=installed_live_provider_run_id,
        installed_live_provider_run_attempt=installed_live_provider_run_attempt,
        untrusted_workspace_run_id=untrusted_workspace_run_id,
        untrusted_workspace_run_attempt=untrusted_workspace_run_attempt,
        remote_acceptance_run_id=remote_acceptance_run_id,
        remote_acceptance_wsl_run_attempt=remote_acceptance_wsl_run_attempt,
        remote_acceptance_remote_ssh_run_attempt=remote_acceptance_remote_ssh_run_attempt,
        evidence_run_id=evidence_run_id,
        evidence_run_attempt=evidence_run_attempt,
        promotion_run_id=promotion_run_id,
        promotion_run_attempt=promotion_run_attempt,
    )
    if observed != expected:
        raise PromotionBundleValidationError(
            "Promotion inputs changed after protected validation or receipt identity mismatched."
        )


def _inventory_hashes(directory: Path) -> dict[str, dict[str, Any]]:
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise PromotionBundleValidationError(
            f"Receipt inventory directory is unavailable: {directory}"
        ) from exc
    if any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise PromotionBundleValidationError("Receipt inventories must contain regular files only.")
    return {
        entry.name: {
            "sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "size": entry.stat().st_size,
        }
        for entry in entries
    }


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionBundleValidationError(f"{label.title()} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise PromotionBundleValidationError(f"{label.title()} must be a JSON object: {path}")
    return payload


def _validate_sbom(path: Path, *, target: str, candidate_digest: str) -> None:
    payload = _json_object(path, "VSIX SBOM")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise PromotionBundleValidationError("VSIX SBOM must be CycloneDX 1.5.")
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        raise PromotionBundleValidationError("VSIX SBOM root component is missing.")
    hashes = component.get("hashes")
    matching_hashes = (
        [value for value in hashes if isinstance(value, dict) and value.get("alg") == "SHA-256"]
        if isinstance(hashes, list)
        else []
    )
    if matching_hashes != [{"alg": "SHA-256", "content": candidate_digest}]:
        raise PromotionBundleValidationError("VSIX SBOM does not bind the exact candidate hash.")
    properties = component.get("properties")
    matching_targets = (
        [
            value.get("value")
            for value in properties
            if isinstance(value, dict) and value.get("name") == "alysis:platform-target"
        ]
        if isinstance(properties, list)
        else []
    )
    if matching_targets != [target]:
        raise PromotionBundleValidationError("VSIX SBOM does not bind the exact platform target.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("managed_runtime_dir", type=Path)
    parser.add_argument("installed_live_provider_dir", type=Path)
    parser.add_argument("untrusted_workspace_dir", type=Path)
    parser.add_argument("remote_acceptance_dir", type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--candidate-run-attempt", required=True, type=int)
    parser.add_argument("--installed-live-provider-run-id", required=True, type=int)
    parser.add_argument("--installed-live-provider-run-attempt", required=True, type=int)
    parser.add_argument("--untrusted-workspace-run-id", required=True, type=int)
    parser.add_argument("--untrusted-workspace-run-attempt", required=True, type=int)
    parser.add_argument("--remote-acceptance-run-id", required=True, type=int)
    parser.add_argument("--remote-acceptance-wsl-run-attempt", required=True, type=int)
    parser.add_argument("--remote-acceptance-remote-ssh-run-attempt", required=True, type=int)
    parser.add_argument("--evidence-run-id", required=True, type=int)
    parser.add_argument("--evidence-run-attempt", required=True, type=int)
    parser.add_argument("--promotion-run-id", type=int)
    parser.add_argument("--promotion-run-attempt", type=int)
    receipt_group = parser.add_mutually_exclusive_group()
    receipt_group.add_argument("--write-receipt", type=Path)
    receipt_group.add_argument("--verify-receipt", type=Path)
    args = parser.parse_args()
    try:
        packages = validate_promotion_bundle(
            args.candidate_dir,
            args.evidence_dir,
            args.managed_runtime_dir,
            args.installed_live_provider_dir,
            args.untrusted_workspace_dir,
            args.remote_acceptance_dir,
            channel=args.channel,
            release_tag=args.release_tag,
            source_sha=args.source_sha,
            candidate_run_id=args.candidate_run_id,
            candidate_run_attempt=args.candidate_run_attempt,
            installed_live_provider_run_id=args.installed_live_provider_run_id,
            installed_live_provider_run_attempt=args.installed_live_provider_run_attempt,
            untrusted_workspace_run_id=args.untrusted_workspace_run_id,
            untrusted_workspace_run_attempt=args.untrusted_workspace_run_attempt,
            remote_acceptance_run_id=args.remote_acceptance_run_id,
            remote_acceptance_wsl_run_attempt=args.remote_acceptance_wsl_run_attempt,
            remote_acceptance_remote_ssh_run_attempt=(
                args.remote_acceptance_remote_ssh_run_attempt
            ),
        )
        if args.write_receipt:
            receipt = build_validation_receipt(
                args.candidate_dir,
                args.evidence_dir,
                args.managed_runtime_dir,
                args.installed_live_provider_dir,
                args.untrusted_workspace_dir,
                args.remote_acceptance_dir,
                channel=args.channel,
                release_tag=args.release_tag,
                source_sha=args.source_sha,
                candidate_run_id=args.candidate_run_id,
                candidate_run_attempt=args.candidate_run_attempt,
                installed_live_provider_run_id=args.installed_live_provider_run_id,
                installed_live_provider_run_attempt=args.installed_live_provider_run_attempt,
                untrusted_workspace_run_id=args.untrusted_workspace_run_id,
                untrusted_workspace_run_attempt=args.untrusted_workspace_run_attempt,
                remote_acceptance_run_id=args.remote_acceptance_run_id,
                remote_acceptance_wsl_run_attempt=args.remote_acceptance_wsl_run_attempt,
                remote_acceptance_remote_ssh_run_attempt=(
                    args.remote_acceptance_remote_ssh_run_attempt
                ),
                evidence_run_id=args.evidence_run_id,
                evidence_run_attempt=args.evidence_run_attempt,
                promotion_run_id=args.promotion_run_id,
                promotion_run_attempt=args.promotion_run_attempt,
            )
            args.write_receipt.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        if args.verify_receipt:
            verify_validation_receipt(
                args.verify_receipt,
                args.candidate_dir,
                args.evidence_dir,
                args.managed_runtime_dir,
                args.installed_live_provider_dir,
                args.untrusted_workspace_dir,
                args.remote_acceptance_dir,
                channel=args.channel,
                release_tag=args.release_tag,
                source_sha=args.source_sha,
                candidate_run_id=args.candidate_run_id,
                candidate_run_attempt=args.candidate_run_attempt,
                installed_live_provider_run_id=args.installed_live_provider_run_id,
                installed_live_provider_run_attempt=args.installed_live_provider_run_attempt,
                untrusted_workspace_run_id=args.untrusted_workspace_run_id,
                untrusted_workspace_run_attempt=args.untrusted_workspace_run_attempt,
                remote_acceptance_run_id=args.remote_acceptance_run_id,
                remote_acceptance_wsl_run_attempt=args.remote_acceptance_wsl_run_attempt,
                remote_acceptance_remote_ssh_run_attempt=(
                    args.remote_acceptance_remote_ssh_run_attempt
                ),
                evidence_run_id=args.evidence_run_id,
                evidence_run_attempt=args.evidence_run_attempt,
                promotion_run_id=args.promotion_run_id,
                promotion_run_attempt=args.promotion_run_attempt,
            )
    except PromotionBundleValidationError as exc:
        print(f"VS Code promotion bundle validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "VS Code promotion bundle validation passed: " + ", ".join(path.name for path in packages)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
