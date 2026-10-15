from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

from scripts.qa.validate_vscode_manual_provider_report import (
    ManualReportValidationError,
    validate_candidate_binding,
)


class UntrustedWorkspaceReportError(ValueError):
    """Raised when retained untrusted-workspace evidence is incomplete or unsafe."""


SCHEMA_NAME = "installed-production-vsix-untrusted-workspace"
SCHEMA_VERSION = 2
SOURCE_REPOSITORY = "https://github.com/AlysisAi/AlysisCode"
APPROVED_VSCODE_VERSION = "1.90.0"
CHECK_IDS = frozenset(
    {
        "empty_profile",
        "workspace_trust_enabled",
        "workspace_actually_untrusted",
        "local_file_workspace",
        "packaged_extension_production_mode",
        "malicious_workspace_cli_present",
        "malicious_workspace_cli_ignored",
        "malicious_workspace_cli_not_executed",
        "signed_managed_runtime_selected",
        "managed_runtime_health",
        "exact_installed_inventory",
    }
)
TARGET_HOST = {
    "win32-x64": ("win32", "x64"),
    "win32-arm64": ("win32", "arm64"),
    "darwin-x64": ("darwin", "x64"),
    "darwin-arm64": ("darwin", "arm64"),
    "linux-x64": ("linux", "x64"),
    "linux-arm64": ("linux", "arm64"),
}
REPORT_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "status",
        "mode",
        "started_at",
        "completed_at",
        "release_tag",
        "source_repository",
        "source_sha",
        "candidate_run_id",
        "workflow_run_id",
        "workflow_run_attempt",
        "platform_target",
        "vsix",
        "vsix_sha256",
        "extension_version",
        "vscode_version",
        "host_platform",
        "host_arch",
        "remote_name",
        "workspace_trusted",
        "workspace_scheme",
        "workspace_authority",
        "workspace_trust_mode",
        "trust_database_seeded",
        "extension_mode",
        "runtime_origin",
        "runtime_production",
        "managed_artifact_version",
        "managed_cli_version",
        "managed_protocol_version",
        "managed_cli_sha256",
        "release_signature_verified",
        "workspace_cli_override_scope",
        "workspace_cli_override_present",
        "workspace_cli_override_ignored",
        "cli_path_override",
        "malicious_cli_executed",
        "installed_inventory",
        "checks",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
_SAFE_IDENTIFIER = re.compile(r"^[0-9A-Za-z._+:-]{1,160}$")
_RFC3339_UTC = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})(?P<fraction>\.\d{3})?Z$"
)


def validate_report(report: dict[str, Any]) -> None:
    if set(report) != REPORT_FIELDS:
        missing = sorted(REPORT_FIELDS - set(report))
        unexpected = sorted(set(report) - REPORT_FIELDS)
        raise UntrustedWorkspaceReportError(
            f"Report fields are not exact; missing={missing}, unexpected={unexpected}."
        )
    _equal(report, "schema_name", SCHEMA_NAME)
    _equal(report, "schema_version", SCHEMA_VERSION)
    _equal(report, "status", "passed")
    _equal(report, "mode", SCHEMA_NAME)
    _equal(report, "source_repository", SOURCE_REPOSITORY)
    _equal(report, "remote_name", "")
    _equal(report, "workspace_trusted", False)
    _equal(report, "workspace_scheme", "file")
    _equal(report, "workspace_authority", "")
    _equal(report, "workspace_trust_mode", "enabled")
    _equal(report, "trust_database_seeded", False)
    _equal(report, "extension_mode", "production")
    _equal(report, "runtime_origin", "managed")
    _equal(report, "runtime_production", True)
    _equal(report, "release_signature_verified", True)
    _equal(report, "workspace_cli_override_present", True)
    _equal(report, "workspace_cli_override_ignored", True)
    _equal(report, "cli_path_override", "")
    _equal(report, "malicious_cli_executed", False)
    _equal(report, "vscode_version", APPROVED_VSCODE_VERSION)

    release_tag = _matching_text(report, "release_tag", _RELEASE_TAG)
    source_sha = _matching_text(report, "source_sha", _SOURCE_SHA)
    del release_tag, source_sha
    candidate_run_id = report.get("candidate_run_id")
    if (
        not isinstance(candidate_run_id, int)
        or isinstance(candidate_run_id, bool)
        or candidate_run_id < 1
    ):
        raise UntrustedWorkspaceReportError("candidate_run_id must be a positive integer.")
    for field in ("workflow_run_id", "workflow_run_attempt"):
        value = report.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise UntrustedWorkspaceReportError(f"{field} must be a positive integer.")
    target = report.get("platform_target")
    if target not in TARGET_HOST:
        raise UntrustedWorkspaceReportError("platform_target is unsupported.")
    expected_host = TARGET_HOST[str(target)]
    if (report.get("host_platform"), report.get("host_arch")) != expected_host:
        raise UntrustedWorkspaceReportError(
            "host_platform and host_arch must match platform_target."
        )
    expected_vsix = f"vscode-alysis-{target}.vsix"
    _equal(report, "vsix", expected_vsix)
    _matching_text(report, "vsix_sha256", _SHA256)
    extension_version = _matching_text(report, "extension_version", _VERSION)
    _matching_text(report, "managed_cli_version", _VERSION)
    _matching_text(report, "managed_cli_sha256", _SHA256)
    _matching_text(report, "managed_artifact_version", _SAFE_IDENTIFIER)
    _matching_text(report, "managed_protocol_version", _SAFE_IDENTIFIER)
    if report.get("workspace_cli_override_scope") not in {"workspace", "workspace-folder"}:
        raise UntrustedWorkspaceReportError("workspace_cli_override_scope is invalid.")
    inventory = report.get("installed_inventory")
    if inventory != [f"alysisai.vscode-alysis@{extension_version}"]:
        raise UntrustedWorkspaceReportError("installed_inventory is not exact.")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != CHECK_IDS:
        raise UntrustedWorkspaceReportError("checks must contain the exact required gate set.")
    if any(value != "passed" for value in checks.values()):
        raise UntrustedWorkspaceReportError("Every untrusted-workspace check must pass.")
    started_at = _timestamp(report, "started_at")
    completed_at = _timestamp(report, "completed_at")
    if completed_at < started_at:
        raise UntrustedWorkspaceReportError("completed_at must not precede started_at.")


def validate_candidate(
    report: dict[str, Any],
    candidate_vsix: Path,
    *,
    trusted_public_key_path: Path | None = None,
    channel: str = "stable",
) -> None:
    validate_report(report)
    try:
        validate_candidate_binding(
            report,
            candidate_vsix,
            channel=channel,
            trusted_public_key_path=trusted_public_key_path,
        )
    except ManualReportValidationError as exc:
        raise UntrustedWorkspaceReportError(
            f"Untrusted-workspace evidence is not candidate-bound: {exc}"
        ) from exc


def _timestamp(report: dict[str, Any], field: str) -> dt.datetime:
    value = report.get(field)
    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise UntrustedWorkspaceReportError(f"{field} must be canonical RFC3339 UTC.")
    try:
        return dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise UntrustedWorkspaceReportError(f"{field} must be canonical RFC3339 UTC.") from exc


def _matching_text(report: dict[str, Any], field: str, pattern: re.Pattern[str]) -> str:
    value = report.get(field)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise UntrustedWorkspaceReportError(f"{field} is invalid.")
    return value


def _equal(report: dict[str, Any], field: str, expected: Any) -> None:
    if report.get(field) != expected:
        raise UntrustedWorkspaceReportError(f"{field} must be {expected!r}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate installed-VSIX evidence from an actually untrusted workspace."
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("candidate_vsix", type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-attempt", required=True, type=int)
    parser.add_argument("--target", required=True, choices=sorted(TARGET_HOST))
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.report.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise UntrustedWorkspaceReportError("Report must be a JSON object.")
        validate_candidate(payload, args.candidate_vsix, channel=args.channel)
        expected_identity = {
            "release_tag": args.release_tag,
            "source_sha": args.source_sha,
            "candidate_run_id": args.candidate_run_id,
            "workflow_run_id": args.workflow_run_id,
            "workflow_run_attempt": args.workflow_run_attempt,
            "platform_target": args.target,
        }
        for field, expected in expected_identity.items():
            _equal(payload, field, expected)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        UntrustedWorkspaceReportError,
    ) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
