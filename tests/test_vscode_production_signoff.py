from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.qa.validate_vscode_production_signoff import (
    ProductionSignoffError,
    validate_signoff,
)
from scripts.qa.vscode_extension_dogfood import SUPPORTED_PLATFORM_TARGETS


def _candidates(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    directory = tmp_path / "candidates"
    directory.mkdir()
    hashes: dict[str, str] = {}
    for target in SUPPORTED_PLATFORM_TARGETS:
        path = directory / f"vscode-alysis-{target}.vsix"
        path.write_bytes(f"candidate:{target}".encode())
        hashes[target] = hashlib.sha256(path.read_bytes()).hexdigest()
    return directory, hashes


def _report(hashes: dict[str, str]) -> dict[str, object]:
    return {
        "schema_name": "vscode-production-signoff-and-rollback",
        "schema_version": 2,
        "status": "approved",
        "release_tag": "v1.2.3",
        "source_sha": "a" * 40,
        "candidate_run_id": 101,
        "channel": "marketplace-stable",
        "completed_at": "2026-07-30T12:00:00Z",
        "approvers": {
            "release_manager": "release-owner",
            "engineering": "engineering-owner",
            "security_governance": "security-owner",
        },
        "candidate_vsix_sha256": hashes,
        "rollback": {
            "owner": "release-owner",
            "trigger_conditions": [
                "credential_exposure",
                "managed_runtime_signature_failure",
                "marketplace_install_failure",
            ],
            "communication_action": "github_release_and_security_advisory",
            "remediation_issue": "https://github.com/AlysisAi/AlysisCode/issues/123",
            "marketplace_action": "unpublish_affected_version",
            "managed_cli_action": "restore_last_known_good_compatible_runtime",
            "post_action_verification": [
                "fresh_install_smoke_passed",
                "managed_runtime_last_known_good_verified",
                "marketplace_version_absent_or_replaced",
                "user_notice_published",
            ],
        },
    }


def _validate(report: dict[str, object], candidates: Path) -> None:
    validate_signoff(
        report,
        candidates,
        expected_release_tag="v1.2.3",
        expected_source_sha="a" * 40,
        expected_candidate_run_id=101,
    )


def test_production_signoff_binds_governance_and_all_six_candidates(tmp_path: Path) -> None:
    candidates, hashes = _candidates(tmp_path)
    _validate(_report(hashes), candidates)


def test_production_signoff_accepts_the_owner_authorized_single_maintainer(tmp_path: Path) -> None:
    candidates, hashes = _candidates(tmp_path)
    report = _report(hashes)
    report["approvers"] = dict.fromkeys(report["approvers"], "Perdikis10")
    report["rollback"]["owner"] = "Perdikis10"
    _validate(report, candidates)


def test_beta_signoff_cannot_authorize_stable_promotion(tmp_path: Path) -> None:
    candidates, hashes = _candidates(tmp_path)
    report = _report(hashes)
    report["channel"] = "marketplace-beta"
    validate_signoff(
        report,
        candidates,
        expected_release_tag="v1.2.3",
        expected_source_sha="a" * 40,
        expected_candidate_run_id=101,
        channel="beta",
    )
    with pytest.raises(ProductionSignoffError, match="stable channel"):
        _validate(report, candidates)


@pytest.mark.parametrize(
    "field,value", [("channel", "marketplace-pre-release"), ("schema_version", 1)]
)
def test_stable_signoff_rejects_legacy_prerelease_approval(
    tmp_path: Path, field: str, value: object
) -> None:
    candidates, hashes = _candidates(tmp_path)
    report = _report(hashes)
    report[field] = value
    with pytest.raises(ProductionSignoffError):
        _validate(report, candidates)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (("candidate_run_id",), "candidate identity"),
        (("candidate_vsix_sha256", "linux-x64"), "linux-x64 VSIX bytes"),
        (("approvers", "engineering"), "distinct"),
        (("rollback", "trigger_conditions"), "trigger conditions"),
        (("rollback", "marketplace_action"), "Marketplace action"),
        (("rollback", "managed_cli_action"), "managed CLI"),
        (("rollback", "post_action_verification"), "post-action"),
        (("rollback", "remediation_issue"), "official issue URL"),
    ),
)
def test_production_signoff_rejects_substitution_or_placeholder_actions(
    tmp_path: Path, mutation: tuple[str, ...], message: str
) -> None:
    candidates, hashes = _candidates(tmp_path)
    report = deepcopy(_report(hashes))
    if mutation == ("candidate_run_id",):
        report["candidate_run_id"] = 999
    elif mutation == ("candidate_vsix_sha256", "linux-x64"):
        report["candidate_vsix_sha256"]["linux-x64"] = "b" * 64  # type: ignore[index]
    elif mutation == ("approvers", "engineering"):
        report["approvers"]["engineering"] = "release-owner"  # type: ignore[index]
    elif mutation == ("rollback", "trigger_conditions"):
        report["rollback"]["trigger_conditions"] = ["something vague"]  # type: ignore[index]
    elif mutation == ("rollback", "marketplace_action"):
        report["rollback"]["marketplace_action"] = "TBD"  # type: ignore[index]
    elif mutation == ("rollback", "managed_cli_action"):
        report["rollback"]["managed_cli_action"] = "none"  # type: ignore[index]
    elif mutation == ("rollback", "post_action_verification"):
        report["rollback"]["post_action_verification"] = []  # type: ignore[index]
    else:
        report["rollback"]["remediation_issue"] = "https://example.com/tbd"  # type: ignore[index]
    with pytest.raises(ProductionSignoffError, match=message):
        _validate(report, candidates)


def test_production_signoff_rejects_secret_looking_values(tmp_path: Path) -> None:
    candidates, hashes = _candidates(tmp_path)
    report = _report(hashes)
    report["rollback"]["owner"] = "password=hidden"  # type: ignore[index]
    with pytest.raises(ProductionSignoffError):
        _validate(report, candidates)
