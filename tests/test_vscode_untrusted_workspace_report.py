from __future__ import annotations

from typing import Any

import pytest

from scripts.qa.validate_vscode_untrusted_workspace_report import (
    CHECK_IDS,
    UntrustedWorkspaceReportError,
    validate_candidate,
    validate_report,
)


def test_untrusted_workspace_report_accepts_exact_safe_machine_evidence() -> None:
    validate_report(_valid_report())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workspace_trusted", True, "workspace_trusted"),
        ("workspace_trust_mode", "disabled", "workspace_trust_mode"),
        ("trust_database_seeded", True, "trust_database_seeded"),
        ("extension_mode", "development", "extension_mode"),
        ("runtime_origin", "override", "runtime_origin"),
        ("workspace_cli_override_ignored", False, "workspace_cli_override_ignored"),
        ("malicious_cli_executed", True, "malicious_cli_executed"),
        ("host_arch", "arm64", "host_platform and host_arch"),
        ("vscode_version", "1.130.0", "vscode_version"),
        ("vsix_sha256", "short", "vsix_sha256"),
        ("candidate_run_id", 0, "candidate_run_id"),
        ("workflow_run_id", 0, "workflow_run_id"),
        ("workflow_run_attempt", 0, "workflow_run_attempt"),
    ],
)
def test_untrusted_workspace_report_rejects_unproven_boundaries(
    field: str, value: object, message: str
) -> None:
    report = _valid_report()
    report[field] = value

    with pytest.raises(UntrustedWorkspaceReportError, match=message):
        validate_report(report)


def test_untrusted_workspace_report_rejects_extra_path_bearing_fields() -> None:
    report = _valid_report()
    report["workspace_path"] = "/home/runner/work/private-project"

    with pytest.raises(UntrustedWorkspaceReportError, match="unexpected=.*workspace_path"):
        validate_report(report)


def test_untrusted_workspace_report_requires_exact_inventory_and_checks() -> None:
    report = _valid_report()
    report["installed_inventory"] = [
        "alysisai.vscode-alysis@0.1.1",
        "unrelated.extension@1.0.0",
    ]
    with pytest.raises(UntrustedWorkspaceReportError, match="installed_inventory"):
        validate_report(report)

    report = _valid_report()
    report["checks"]["fabricated"] = "passed"
    with pytest.raises(UntrustedWorkspaceReportError, match="exact required gate set"):
        validate_report(report)


def test_untrusted_workspace_report_must_bind_to_real_candidate_bytes(tmp_path) -> None:
    with pytest.raises(UntrustedWorkspaceReportError, match="not candidate-bound"):
        validate_candidate(_valid_report(), tmp_path / "missing.vsix")


def _valid_report() -> dict[str, Any]:
    return {
        "schema_name": "installed-production-vsix-untrusted-workspace",
        "schema_version": 2,
        "status": "passed",
        "mode": "installed-production-vsix-untrusted-workspace",
        "started_at": "2026-07-30T10:00:00.000Z",
        "completed_at": "2026-07-30T10:01:00.000Z",
        "release_tag": "v0.1.1",
        "source_repository": "https://github.com/AlysisAi/AlysisCode",
        "source_sha": "a" * 40,
        "candidate_run_id": 123,
        "workflow_run_id": 456,
        "workflow_run_attempt": 2,
        "platform_target": "linux-x64",
        "vsix": "vscode-alysis-linux-x64.vsix",
        "vsix_sha256": "b" * 64,
        "extension_version": "0.1.1",
        "vscode_version": "1.90.0",
        "host_platform": "linux",
        "host_arch": "x64",
        "remote_name": "",
        "workspace_trusted": False,
        "workspace_scheme": "file",
        "workspace_authority": "",
        "workspace_trust_mode": "enabled",
        "trust_database_seeded": False,
        "extension_mode": "production",
        "runtime_origin": "managed",
        "runtime_production": True,
        "managed_artifact_version": "cli-0.1.1-linux-x64",
        "managed_cli_version": "0.1.1",
        "managed_protocol_version": "1",
        "managed_cli_sha256": "c" * 64,
        "release_signature_verified": True,
        "workspace_cli_override_scope": "workspace-folder",
        "workspace_cli_override_present": True,
        "workspace_cli_override_ignored": True,
        "cli_path_override": "",
        "malicious_cli_executed": False,
        "installed_inventory": ["alysisai.vscode-alysis@0.1.1"],
        "checks": {check: "passed" for check in CHECK_IDS},
    }
