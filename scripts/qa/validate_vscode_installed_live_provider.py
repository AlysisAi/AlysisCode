"""Validate redacted evidence from real-provider QA against an installed production VSIX."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    validate_production_dogfood_candidate_binding,
)

SCHEMA_NAME = "installed-production-vsix-live-provider"
SCHEMA_VERSION = 2
TARGET_HOSTS = {
    "win32-x64": ("win32", "x64"),
    "win32-arm64": ("win32", "arm64"),
    "darwin-x64": ("darwin", "x64"),
    "darwin-arm64": ("darwin", "arm64"),
    "linux-x64": ("linux", "x64"),
    "linux-arm64": ("linux", "arm64"),
}
RECEIPTS = {
    "clean_install",
    "native_signature",
    "release_attestations",
    "managed_manifest_signature",
    "managed_runtime_health",
    "provider_secret_storage",
    "provider_secret_cleared",
    "provider_origin_policy",
    "restart_profile_reused",
    "restart_secret_absent",
    "exact_secret_artifact_scan",
}
REPORT_FIELDS = {
    "schema_name",
    "schema_version",
    "status",
    "mode",
    "started_at",
    "completed_at",
    "release_tag",
    "source_sha",
    "candidate_run_id",
    "workflow_run_id",
    "workflow_run_attempt",
    "production_dogfood_sha256",
    "platform_target",
    "vsix_sha256",
    "extension_version",
    "vscode_version",
    "host_platform",
    "host_arch",
    "extension_mode",
    "runtime_origin",
    "runtime_production",
    "managed_artifact_version",
    "managed_cli_version",
    "managed_protocol_version",
    "managed_cli_sha256",
    "source_repository",
    "provider",
    "model",
    "provider_origin",
    "provider_origin_policy_sha256",
    "readonly_mode",
    "response",
    "receipts",
    "extension_host_launches",
    "retained_response_content",
}
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
TAG = re.compile(r"^v\d+\.\d+\.\d+$")
SAFE_EVIDENCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+ ()-]*$")
SECRET_LIKE_EVIDENCE = re.compile(
    r"(?:api[_ -]?key|access[_ -]?token|bearer|secret)\s*[:=]|(?:^|\s)sk-[A-Za-z0-9_-]{16,}(?:\s|$)",
    re.IGNORECASE,
)
RUNTIME_IDENTITY_FIELDS = (
    "extension_version",
    "vscode_version",
    "host_platform",
    "host_arch",
    "extension_mode",
    "runtime_origin",
    "runtime_production",
    "managed_artifact_version",
    "managed_cli_version",
    "managed_protocol_version",
    "managed_cli_sha256",
    "release_tag",
    "source_repository",
    "source_sha",
    "platform_target",
)


class InstalledLiveProviderEvidenceError(ValueError):
    """Raised when retained live-provider evidence is incomplete or unbound."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InstalledLiveProviderEvidenceError(f"{name} must be an object")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstalledLiveProviderEvidenceError(f"{name} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, name: str) -> datetime:
    rendered = _nonempty(value, name)
    if not rendered.endswith("Z"):
        raise InstalledLiveProviderEvidenceError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(rendered[:-1] + "+00:00")
    except ValueError as error:
        raise InstalledLiveProviderEvidenceError(
            f"{name} must be an RFC3339 UTC timestamp"
        ) from error
    return parsed


def _safe_evidence_identifier(value: object, name: str, maximum_length: int) -> str:
    rendered = _nonempty(value, name)
    if (
        len(rendered) > maximum_length
        or not rendered.isascii()
        or SAFE_EVIDENCE_IDENTIFIER.fullmatch(rendered) is None
        or SECRET_LIKE_EVIDENCE.search(rendered) is not None
    ):
        raise InstalledLiveProviderEvidenceError(f"{name} is not safe retained evidence")
    return rendered


def _provider_origin(value: object) -> str:
    rendered = _nonempty(value, "provider_origin")
    if len(rendered) > 255 or not rendered.isascii():
        raise InstalledLiveProviderEvidenceError("provider_origin is invalid")
    try:
        parsed = urlsplit(rendered)
        port = parsed.port
    except ValueError as error:
        raise InstalledLiveProviderEvidenceError("provider_origin is invalid") from error
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
        or port is not None
        or not hostname
    ):
        raise InstalledLiveProviderEvidenceError(
            "provider_origin must be a canonical default-port HTTPS origin"
        )
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if rendered != f"https://{authority}" or hostname != hostname.lower() or hostname.endswith("."):
        raise InstalledLiveProviderEvidenceError("provider_origin is not canonical")
    normalized_hostname = hostname.lower()
    if (
        normalized_hostname == "localhost"
        or normalized_hostname.endswith(".localhost")
        or normalized_hostname.endswith(".local")
        or normalized_hostname.endswith(".internal")
    ):
        raise InstalledLiveProviderEvidenceError("provider_origin is not public")
    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        if "." not in normalized_hostname:
            raise InstalledLiveProviderEvidenceError("provider_origin is not public") from None
    else:
        if not address.is_global:
            raise InstalledLiveProviderEvidenceError("provider_origin is not public")
    return rendered


def validate_installed_live_provider_evidence(
    report: dict[str, Any],
    *,
    candidate_vsix: Path,
    production_dogfood: Path,
    expected_target: str,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: str,
    expected_workflow_run_id: int,
    expected_workflow_run_attempt: int,
    provider_origin_policy: Path,
    channel: str = "stable",
) -> None:
    """Validate a single target's redacted live-provider evidence and byte bindings."""
    if expected_target not in TARGET_HOSTS:
        raise InstalledLiveProviderEvidenceError("expected target is unsupported")
    if set(report) != REPORT_FIELDS:
        raise InstalledLiveProviderEvidenceError(
            "installed live-provider report fields are not exact"
        )
    if report.get("schema_name") != SCHEMA_NAME or report.get("schema_version") != SCHEMA_VERSION:
        raise InstalledLiveProviderEvidenceError(
            "installed live-provider schema identity is invalid"
        )
    if report.get("status") != "passed" or report.get("mode") != SCHEMA_NAME:
        raise InstalledLiveProviderEvidenceError("installed live-provider status is not passed")
    started = _timestamp(report.get("started_at"), "started_at")
    completed = _timestamp(report.get("completed_at"), "completed_at")
    if completed < started:
        raise InstalledLiveProviderEvidenceError("completed_at precedes started_at")
    if (completed - started).total_seconds() > 45 * 60:
        raise InstalledLiveProviderEvidenceError("live-provider run exceeded the evidence window")

    expected_identity = {
        "release_tag": expected_release_tag,
        "source_sha": expected_source_sha,
        "candidate_run_id": expected_candidate_run_id,
        "workflow_run_id": expected_workflow_run_id,
        "workflow_run_attempt": expected_workflow_run_attempt,
        "platform_target": expected_target,
    }
    for field, expected in expected_identity.items():
        if report.get(field) != expected:
            raise InstalledLiveProviderEvidenceError(
                f"{field} does not match the dispatch identity"
            )
    if not TAG.fullmatch(expected_release_tag) or not HEX_40.fullmatch(expected_source_sha):
        raise InstalledLiveProviderEvidenceError("expected release identity is invalid")
    if not expected_candidate_run_id.isdigit() or int(expected_candidate_run_id) <= 0:
        raise InstalledLiveProviderEvidenceError("expected candidate run id is invalid")
    if expected_workflow_run_id <= 0 or expected_workflow_run_attempt <= 0:
        raise InstalledLiveProviderEvidenceError("expected workflow run identity is invalid")

    candidate_vsix = candidate_vsix.resolve(strict=True)
    production_dogfood = production_dogfood.resolve(strict=True)
    dogfood_payload = _mapping(
        json.loads(production_dogfood.read_text(encoding="utf-8")),
        "production dogfood",
    )
    validate_production_dogfood_candidate_binding(
        dogfood_payload,
        candidate_vsix,
        channel=channel,
        require_vscode_compatibility=True,
    )
    byte_bindings = {
        "vsix_sha256": _sha256(candidate_vsix),
        "production_dogfood_sha256": _sha256(production_dogfood),
    }
    for field, expected in byte_bindings.items():
        if report.get(field) != expected:
            raise InstalledLiveProviderEvidenceError(f"{field} does not bind the exact candidate")

    expected_platform, expected_arch = TARGET_HOSTS[expected_target]
    if report.get("host_platform") != expected_platform or report.get("host_arch") != expected_arch:
        raise InstalledLiveProviderEvidenceError("host platform/architecture does not match target")
    for field in (
        "extension_version",
        "vscode_version",
        "managed_artifact_version",
        "managed_cli_version",
        "managed_protocol_version",
    ):
        _nonempty(report.get(field), field)
    provider = _safe_evidence_identifier(report.get("provider"), "provider", 128)
    _safe_evidence_identifier(report.get("model"), "model", 256)
    provider_origin = _provider_origin(report.get("provider_origin"))
    from scripts.qa.validate_vscode_live_provider_policy import (
        LiveProviderPolicyError,
        authorize,
    )

    try:
        policy_sha256 = authorize(
            provider_origin_policy.absolute(),
            provider=provider,
            origin=provider_origin,
        )
    except LiveProviderPolicyError as exc:
        raise InstalledLiveProviderEvidenceError(str(exc)) from exc
    if report.get("provider_origin_policy_sha256") != policy_sha256:
        raise InstalledLiveProviderEvidenceError(
            "provider_origin_policy_sha256 does not bind the source-controlled policy"
        )
    if not SEMVER.fullmatch(str(report["extension_version"])):
        raise InstalledLiveProviderEvidenceError("extension_version is invalid")
    if report.get("extension_mode") != "production":
        raise InstalledLiveProviderEvidenceError("extension_mode must be production")
    if report.get("runtime_origin") != "managed" or report.get("runtime_production") is not True:
        raise InstalledLiveProviderEvidenceError("runtime must be managed production")
    if report.get("source_repository") != "https://github.com/AlysisAi/AlysisCode":
        raise InstalledLiveProviderEvidenceError("source_repository is invalid")
    if not HEX_64.fullmatch(str(report.get("managed_cli_sha256", ""))):
        raise InstalledLiveProviderEvidenceError("managed_cli_sha256 is invalid")
    if report.get("readonly_mode") is not True:
        raise InstalledLiveProviderEvidenceError("live provider chat was not readonly")
    for field in RUNTIME_IDENTITY_FIELDS:
        if report.get(field) != dogfood_payload.get(field):
            raise InstalledLiveProviderEvidenceError(
                f"{field} does not match the exact production dogfood runtime"
            )

    response = _mapping(report.get("response"), "response")
    if set(response) != {"length", "sha256", "marker_matched", "elapsed_ms"}:
        raise InstalledLiveProviderEvidenceError("response evidence fields are not exact")
    if not isinstance(response.get("length"), int) or not 0 < response["length"] <= 96:
        raise InstalledLiveProviderEvidenceError("response.length must be between 1 and 96")
    if not HEX_64.fullmatch(str(response.get("sha256", ""))):
        raise InstalledLiveProviderEvidenceError("response.sha256 is invalid")
    if response.get("marker_matched") is not True:
        raise InstalledLiveProviderEvidenceError("real provider response marker did not match")
    elapsed = response.get("elapsed_ms")
    if not isinstance(elapsed, int) or not 0 < elapsed <= 180_000:
        raise InstalledLiveProviderEvidenceError("response.elapsed_ms is invalid")

    receipts = _mapping(report.get("receipts"), "receipts")
    if set(receipts) != RECEIPTS or any(value != "passed" for value in receipts.values()):
        raise InstalledLiveProviderEvidenceError("live-provider receipts are incomplete")
    if report.get("extension_host_launches") != 2:
        raise InstalledLiveProviderEvidenceError("exactly two Extension Host launches are required")
    if report.get("retained_response_content") is not False:
        raise InstalledLiveProviderEvidenceError("response content retention must be disabled")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("candidate_vsix", type=Path)
    parser.add_argument("production_dogfood", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-attempt", required=True, type=int)
    parser.add_argument("--provider-origin-policy", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    validate_installed_live_provider_evidence(
        report,
        channel=args.channel,
        candidate_vsix=args.candidate_vsix,
        production_dogfood=args.production_dogfood,
        expected_target=args.target,
        expected_release_tag=args.release_tag,
        expected_source_sha=args.source_sha,
        expected_candidate_run_id=args.candidate_run_id,
        expected_workflow_run_id=args.workflow_run_id,
        expected_workflow_run_attempt=args.workflow_run_attempt,
        provider_origin_policy=args.provider_origin_policy,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
