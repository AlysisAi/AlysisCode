#!/usr/bin/env python3
"""Validate a closed production signoff and rollback receipt against six candidate VSIX files."""

from __future__ import annotations

import argparse
import hashlib
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
    SUPPORTED_PLATFORM_TARGETS,
)
from scripts.release.vscode_approval_policy import is_single_maintainer_login  # noqa: E402

REPORT_FILENAME = "vscode-alysis-production-signoff.json"
SCHEMA_NAME = "vscode-production-signoff-and-rollback"
SCHEMA_VERSION = 2
REPORT_KEYS = {
    "schema_name",
    "schema_version",
    "status",
    "release_tag",
    "source_sha",
    "candidate_run_id",
    "channel",
    "completed_at",
    "approvers",
    "candidate_vsix_sha256",
    "rollback",
}
APPROVER_KEYS = {"release_manager", "engineering", "security_governance"}
ROLLBACK_KEYS = {
    "owner",
    "trigger_conditions",
    "communication_action",
    "remediation_issue",
    "marketplace_action",
    "managed_cli_action",
    "post_action_verification",
}
ALLOWED_TRIGGERS = {
    "credential_exposure",
    "data_integrity_regression",
    "managed_runtime_signature_failure",
    "marketplace_install_failure",
    "provider_regression",
}
REQUIRED_POST_ACTION = (
    "fresh_install_smoke_passed",
    "managed_runtime_last_known_good_verified",
    "marketplace_version_absent_or_replaced",
    "user_notice_published",
)
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
PLACEHOLDERS = {"example", "n/a", "none", "pending", "placeholder", "tbd", "unknown"}
SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|password|private[_ -]?key|secret)\s*[:=]"
)


class ProductionSignoffError(ValueError):
    """Raised when release governance evidence is incomplete or not candidate-bound."""


def _fail(message: str) -> None:
    raise ProductionSignoffError(message)


def _sha256(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        _fail(f"Candidate must be a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_login(value: object, label: str) -> str:
    if not isinstance(value, str) or LOGIN_RE.fullmatch(value) is None:
        _fail(f"{label} must be a GitHub login-shaped accountable identity.")
    if value.casefold() in PLACEHOLDERS:
        _fail(f"{label} must not be a placeholder.")
    return value


def _assert_secret_free(value: object) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_secret_free(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_secret_free(nested)
    elif isinstance(value, str) and SECRET_RE.search(value):
        _fail("Production signoff contains a secret-looking value.")


def _validate_remediation_issue(value: object) -> None:
    if not isinstance(value, str):
        _fail("rollback.remediation_issue must be an immutable official issue URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or re.fullmatch(r"/AlysisAi/AlysisCode/issues/[1-9][0-9]*", parsed.path) is None
    ):
        _fail("rollback.remediation_issue must be an immutable official issue URL.")


def validate_signoff(
    report: dict[str, Any],
    candidate_dir: Path,
    *,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: int,
    channel: str = "stable",
) -> None:
    if set(report) != REPORT_KEYS:
        _fail("Production signoff fields are not exact.")
    if report.get("schema_name") != SCHEMA_NAME or report.get("schema_version") != SCHEMA_VERSION:
        _fail("Production signoff schema is unsupported.")
    if (
        channel not in {"stable", "beta"}
        or report.get("status") != "approved"
        or report.get("channel") != f"marketplace-{channel}"
    ):
        _fail(f"Production signoff must explicitly approve the Marketplace {channel} channel.")
    if (
        TAG_RE.fullmatch(expected_release_tag) is None
        or report.get("release_tag") != expected_release_tag
        or SOURCE_SHA_RE.fullmatch(expected_source_sha) is None
        or report.get("source_sha") != expected_source_sha
        or expected_candidate_run_id <= 0
        or report.get("candidate_run_id") != expected_candidate_run_id
    ):
        _fail("Production signoff does not match the immutable candidate identity.")
    completed_at = report.get("completed_at")
    if not isinstance(completed_at, str) or UTC_RE.fullmatch(completed_at) is None:
        _fail("completed_at must be a real RFC3339 UTC timestamp.")
    try:
        datetime.fromisoformat(completed_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ProductionSignoffError("completed_at must be a real RFC3339 UTC timestamp.") from exc

    approvers = report.get("approvers")
    if not isinstance(approvers, dict) or set(approvers) != APPROVER_KEYS:
        _fail("Production signoff approver fields are not exact.")
    identities = [_safe_login(approvers[key], f"approvers.{key}") for key in sorted(approvers)]
    normalized = [identity.casefold() for identity in identities]
    if any(
        normalized.count(identity) > 1 and not is_single_maintainer_login(identity)
        for identity in normalized
    ):
        _fail(
            "Release, engineering, and security approvers must be distinct identities "
            "unless approved by the source-pinned single maintainer."
        )

    hashes = report.get("candidate_vsix_sha256")
    targets = set(SUPPORTED_PLATFORM_TARGETS)
    if not isinstance(hashes, dict) or set(hashes) != targets:
        _fail("Production signoff must bind exactly all six target VSIX hashes.")
    for target in sorted(targets):
        reported = hashes.get(target)
        if not isinstance(reported, str) or SHA256_RE.fullmatch(reported) is None:
            _fail(f"Production signoff hash is invalid for {target}.")
        candidate = candidate_dir / f"vscode-alysis-{target}.vsix"
        if reported != _sha256(candidate):
            _fail(f"Production signoff does not bind the exact {target} VSIX bytes.")

    rollback = report.get("rollback")
    if not isinstance(rollback, dict) or set(rollback) != ROLLBACK_KEYS:
        _fail("Production rollback fields are not exact.")
    owner = _safe_login(rollback.get("owner"), "rollback.owner")
    if owner.casefold() not in {identity.casefold() for identity in identities}:
        _fail("Rollback owner must be one of the accountable approvers.")
    triggers = rollback.get("trigger_conditions")
    if (
        not isinstance(triggers, list)
        or not triggers
        or any(not isinstance(value, str) for value in triggers)
        or triggers != sorted(set(triggers))
        or not set(triggers).issubset(ALLOWED_TRIGGERS)
    ):
        _fail("Rollback trigger conditions must be a sorted reviewed policy subset.")
    if rollback.get("communication_action") != "github_release_and_security_advisory":
        _fail("Rollback communication action is incomplete.")
    _validate_remediation_issue(rollback.get("remediation_issue"))
    if rollback.get("marketplace_action") != "unpublish_affected_version":
        _fail("Rollback Marketplace action is incomplete.")
    if rollback.get("managed_cli_action") != "restore_last_known_good_compatible_runtime":
        _fail("Rollback managed CLI compatibility action is incomplete.")
    if rollback.get("post_action_verification") != list(REQUIRED_POST_ACTION):
        _fail("Rollback post-action verification is incomplete.")
    _assert_secret_free(report)


def validate_file(path: Path, candidate_dir: Path, **expected: Any) -> None:
    if not path.is_file() or path.is_symlink():
        _fail("Production signoff must be a regular local JSON file.")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionSignoffError("Production signoff is not valid JSON.") from exc
    if not isinstance(report, dict):
        _fail("Production signoff must be a JSON object.")
    validate_signoff(report, candidate_dir, **expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    args = parser.parse_args(argv)
    try:
        validate_file(
            args.report,
            args.candidate_dir,
            expected_release_tag=args.release_tag,
            expected_source_sha=args.source_sha,
            expected_candidate_run_id=args.candidate_run_id,
            channel=args.channel,
        )
    except ProductionSignoffError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
