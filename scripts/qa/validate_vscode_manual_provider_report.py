#!/usr/bin/env python3
"""Validate completed manual real-provider VS Code dogfood evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME,
    MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION,
    MANUAL_REAL_PROVIDER_REQUIRED_CHECKS,
    SECRET_PATTERNS,
    SUPPORTED_PLATFORM_TARGETS,
    DogfoodError,
    validate_production_vscode_compatibility,
    validate_release_valid_summary,
)
from scripts.release.build_vscode_vsix_sbom import (  # noqa: E402
    VsixSbomError,
    verify_artifact_signature,
)

URL_USERINFO_PATTERN = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
PASSWORD_PATTERN = re.compile(r"(?i)(password|passwd|secret)(\s*[:=]\s*)([^\s,;]+)")
SECRET_VALUE_PATTERNS = (*SECRET_PATTERNS, URL_USERINFO_PATTERN, PASSWORD_PATTERN)
OFFICIAL_SOURCE_REPOSITORY = "https://github.com/AlysisAi/AlysisCode"
RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
IDENTITY_PLACEHOLDERS = {"example", "n/a", "none", "pending", "placeholder", "tbd", "unknown"}
CHECK_RECEIPT_KEYS = frozenset(
    {
        "status",
        "completed_at",
        "artifact_sha256",
        "event_id",
        "notes",
        "rationale",
    }
)

# A completed production report must exercise every release-defining path. These two checks
# require a deliberately different capability or deployment shape, so they may be marked
# not_applicable when the reviewer records why that shape was unavailable for this candidate.
WAIVABLE_CHECK_IDS = frozenset(
    {
        "mcp_oauth_local_lifecycle",
        "run_swarm_capability_gate",
    }
)
NON_WAIVABLE_CHECK_IDS = frozenset(
    {
        "active_cancellation_cooperative",
        "assistant_show_update",
        "broken_cli_recovery",
        "configure_provider_secret_storage",
        "create_session",
        "diffs_artifacts",
        "doctor",
        "execute_plan_review_mode",
        "execute_preview",
        "forge_exec_unsupported_warning",
        "forge_plan_regenerate",
        "forge_plan_state",
        "forge_plan_validate",
        "forge_show_status",
        "goal_show_update",
        "hooks_watch_not_advertised",
        "image_path",
        "install_vsix",
        "manage_tree_domains",
        "managed_runtime_clean_install",
        "missing_provider_recovery",
        "model_info",
        "normal_chat",
        "open_disposable_workspace",
        "paste_image",
        "run_swarm_review_flow",
        "run_task",
        "secret_redaction",
        "slash_plan",
        "subagent_status_on_off",
        "task_show_update",
        "terminals_lifecycle",
        "trace_lifecycle",
    }
)


class ManualReportValidationError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise ManualReportValidationError(message)


def _load_report(path_arg: str) -> dict[str, Any]:
    lowered = path_arg.casefold()
    if lowered.startswith(("http://", "https://")):
        _fail(
            "HTTPS manual real-provider reports are not accepted by this validator. "
            "Export the completed JSON into the checked-out workspace and pass the local path."
        )
    path = Path(path_arg).expanduser()
    if not path.is_file():
        _fail(f"Manual real-provider report does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualReportValidationError(
            f"Manual real-provider report is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        _fail("Manual real-provider report must be a JSON object.")
    return payload


def _string_values(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, child in value.items():
            found.extend(_string_values(child, f"{path}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value):
            found.extend(_string_values(child, f"{path}[{index}]"))
        return found
    if isinstance(value, str):
        return [(path, value)]
    return []


def _assert_no_secret_values(report: dict[str, Any]) -> None:
    for path, value in _string_values(report):
        if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
            _fail(f"Manual real-provider report contains a secret-looking value at {path}.")


def _require_non_empty_string(report: dict[str, Any], field: str) -> None:
    value = report.get(field)
    if not isinstance(value, str) or not value.strip():
        _fail(f"Manual real-provider report field `{field}` must be a non-empty string.")


def _parse_rfc3339_utc(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or RFC3339_UTC_RE.fullmatch(value) is None:
        _fail(f"{field} must be a UTC RFC3339 timestamp ending in `Z`.")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ManualReportValidationError(f"{field} must be a real UTC RFC3339 timestamp.") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        _fail(f"{field} must be a UTC RFC3339 timestamp ending in `Z`.")
    return parsed


def _check_receipt(
    entry: Any,
    check_id: str,
    *,
    report_started_at: dt.datetime,
    report_completed_at: dt.datetime,
) -> tuple[str, str]:
    if not isinstance(entry, dict):
        _fail(
            f"completed_checks.{check_id} must be a structured receipt object; "
            "bare status strings are not accepted."
        )
    unexpected = sorted(set(entry) - CHECK_RECEIPT_KEYS)
    if unexpected:
        _fail(
            f"completed_checks.{check_id} contains unknown receipt fields: " + ", ".join(unexpected)
        )
    status = entry.get("status")
    if status not in {"passed", "not_applicable"}:
        _fail(f"completed_checks.{check_id}.status must be exactly `passed` or `not_applicable`.")
    completed_at = _parse_rfc3339_utc(
        entry.get("completed_at"), f"completed_checks.{check_id}.completed_at"
    )
    if completed_at < report_started_at or completed_at > report_completed_at:
        _fail(f"completed_checks.{check_id}.completed_at must fall within the report interval.")

    artifact_sha256 = entry.get("artifact_sha256")
    event_id = entry.get("event_id")
    if artifact_sha256 is not None and not isinstance(artifact_sha256, str):
        _fail(f"completed_checks.{check_id}.artifact_sha256 must be a string.")
    if event_id is not None and not isinstance(event_id, str):
        _fail(f"completed_checks.{check_id}.event_id must be a string.")
    has_artifact = isinstance(artifact_sha256, str) and bool(artifact_sha256)
    has_event = isinstance(event_id, str) and bool(event_id)
    if not has_artifact or has_event:
        _fail(
            f"completed_checks.{check_id} must contain an immutable artifact_sha256; "
            "free-form event_id locators are not accepted for production signoff."
        )
    if has_artifact and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None:
        _fail(f"completed_checks.{check_id}.artifact_sha256 must be lowercase SHA-256.")

    notes = entry.get("notes", "")
    rationale = entry.get("rationale", "")
    if not isinstance(notes, str):
        _fail(f"completed_checks.{check_id}.notes must be a string.")
    if not isinstance(rationale, str):
        _fail(f"completed_checks.{check_id}.rationale must be a string.")
    return status, rationale


def validate_report(report: dict[str, Any]) -> None:
    if report.get("status") == "manual_steps_written":
        _fail(
            "Generated manual_steps_written dogfood reports are placeholders, not completed evidence."
        )
    if report.get("schema_name") != MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME:
        _fail(
            "Manual real-provider report schema_name must be "
            f"`{MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME}`."
        )
    if report.get("schema_version") != MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION:
        _fail(
            "Manual real-provider report schema_version must be "
            f"{MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION}."
        )
    if report.get("mode") != "manual-real-provider":
        _fail("Manual real-provider report mode must be `manual-real-provider`.")
    if report.get("status") != "completed":
        _fail("Manual real-provider report status must be `completed`.")

    for field in (
        "provider",
        "extension_version",
        "vsix",
        "reviewer",
        "release_tag",
        "source_repository",
        "source_sha",
        "vscode_version",
        "host_platform",
        "host_arch",
        "workspace_scheme",
        "started_at",
        "completed_at",
    ):
        _require_non_empty_string(report, field)

    report_started_at = _parse_rfc3339_utc(report.get("started_at"), "started_at")
    report_completed_at = _parse_rfc3339_utc(report.get("completed_at"), "completed_at")
    if report_completed_at < report_started_at:
        _fail("completed_at must not precede started_at.")

    for field in ("managed_artifact_version",):
        _require_non_empty_string(report, field)
    reviewer = str(report.get("reviewer") or "")
    if GITHUB_LOGIN_RE.fullmatch(reviewer) is None or reviewer.casefold() in IDENTITY_PLACEHOLDERS:
        _fail("reviewer must be a non-placeholder GitHub login-shaped identity.")
    if SEMVER_RE.fullmatch(str(report.get("extension_version") or "")) is None:
        _fail("extension_version must be a semantic version.")
    if re.fullmatch(r"[0-9a-f]{64}", str(report.get("vsix_sha256") or "")) is None:
        _fail("vsix_sha256 must be the lowercase SHA-256 of the exact installed VSIX.")
    candidate_run_id = report.get("candidate_run_id")
    if (
        isinstance(candidate_run_id, bool)
        or not isinstance(candidate_run_id, int)
        or candidate_run_id <= 0
    ):
        _fail("candidate_run_id must identify the positive immutable candidate workflow run.")
    if re.fullmatch(r"v\d+\.\d+\.\d+", str(report.get("release_tag") or "")) is None:
        _fail("release_tag must identify the immutable vX.Y.Z release tag.")
    if report.get("source_repository") != OFFICIAL_SOURCE_REPOSITORY:
        _fail("source_repository must identify the official Alysis Code repository.")
    if re.fullmatch(r"[0-9a-f]{40}", str(report.get("source_sha") or "")) is None:
        _fail("source_sha must identify the exact lowercase 40-character release commit.")
    if re.fullmatch(r"[0-9a-f]{64}", str(report.get("production_dogfood_sha256") or "")) is None:
        _fail("production_dogfood_sha256 must bind the exact automated dogfood report bytes.")
    if report.get("platform_target") not in SUPPORTED_PLATFORM_TARGETS:
        _fail("platform_target must identify a supported target VSIX.")
    if SEMVER_RE.fullmatch(str(report.get("vscode_version") or "")) is None:
        _fail("vscode_version must identify the exercised VS Code build.")
    if report.get("host_platform") not in {"win32", "darwin", "linux"}:
        _fail("host_platform must identify the Extension Host OS.")
    if report.get("host_arch") not in {"x64", "arm64"}:
        _fail("host_arch must identify the Extension Host architecture.")
    target = str(report.get("platform_target") or "")
    expected_platform = (
        "win32"
        if target.startswith("win32-")
        else "darwin"
        if target.startswith("darwin-")
        else "linux"
    )
    expected_arch = "arm64" if target.endswith("-arm64") else "x64"
    if report.get("host_platform") != expected_platform or report.get("host_arch") != expected_arch:
        _fail("host_platform and host_arch must match platform_target.")
    remote_name = report.get("remote_name")
    if not isinstance(remote_name, str) or len(remote_name) > 128:
        _fail("remote_name must be a bounded string, empty for a local Extension Host.")
    if report.get("workspace_trusted") is not True:
        _fail("workspace_trusted must be true for the production real-provider run.")
    if report.get("workspace_scheme") != "file":
        _fail("workspace_scheme must be `file` for target production evidence.")
    workspace_authority = report.get("workspace_authority")
    if not isinstance(workspace_authority, str) or len(workspace_authority) > 512:
        _fail("workspace_authority must be a bounded string.")
    if report.get("runtime_origin") != "managed":
        _fail("runtime_origin must be `managed`.")
    if report.get("runtime_production") is not True:
        _fail("runtime_production must be true.")
    if re.fullmatch(r"[0-9a-f]{64}", str(report.get("managed_cli_sha256") or "")) is None:
        _fail("managed_cli_sha256 must be the lowercase SHA-256 of the selected managed runtime.")
    if report.get("native_signature_check") != "passed":
        _fail("native_signature_check must be `passed`.")
    if report.get("release_signature_check") != "passed":
        _fail("release_signature_check must be `passed`.")
    if report.get("package_install_check") != "passed":
        _fail("package_install_check must be `passed`.")
    if report.get("bridge_health") != "passed":
        _fail("bridge_health must be `passed`.")
    if report.get("cli_path_override") not in (None, ""):
        _fail(
            "cli_path_override must be empty; development CLI overrides are not release evidence."
        )

    if report.get("known_limitations_confirmed") is not True:
        _fail("known_limitations_confirmed must be true.")
    if report.get("no_p0_p1_blockers") is not True:
        _fail("no_p0_p1_blockers must be true.")
    if report.get("secret_leak_check") != "passed":
        _fail("secret_leak_check must be `passed`.")

    completed_checks = report.get("completed_checks")
    if not isinstance(completed_checks, dict):
        _fail("completed_checks must be a JSON object keyed by required check id.")

    required_ids = [check["id"] for check in MANUAL_REAL_PROVIDER_REQUIRED_CHECKS]
    required_id_set = set(required_ids)
    policy_ids = set(NON_WAIVABLE_CHECK_IDS | WAIVABLE_CHECK_IDS)
    if policy_ids != required_id_set or NON_WAIVABLE_CHECK_IDS & WAIVABLE_CHECK_IDS:
        _fail("Manual real-provider check waiver policy is out of sync with required checks.")
    missing = [check_id for check_id in required_ids if check_id not in completed_checks]
    if missing:
        _fail("Manual real-provider report is missing required checks: " + ", ".join(missing))
    unexpected = sorted(set(completed_checks) - required_id_set)
    if unexpected:
        _fail("Manual real-provider report contains unknown checks: " + ", ".join(unexpected))

    invalid: list[str] = []
    waived_non_waivable: list[str] = []
    for check_id in required_ids:
        status, rationale = _check_receipt(
            completed_checks[check_id],
            check_id,
            report_started_at=report_started_at,
            report_completed_at=report_completed_at,
        )
        if status == "passed":
            continue
        if status == "not_applicable":
            if check_id in NON_WAIVABLE_CHECK_IDS:
                waived_non_waivable.append(check_id)
                continue
            if check_id in WAIVABLE_CHECK_IDS and rationale.strip():
                continue
        invalid.append(check_id)
    if waived_non_waivable:
        _fail("Non-waivable required checks must be `passed`: " + ", ".join(waived_non_waivable))
    if invalid:
        _fail(
            "Required checks must be `passed`; explicitly waivable checks may be "
            "`not_applicable` with rationale: " + ", ".join(invalid)
        )

    _assert_no_secret_values(report)


def _read_unique(archive: ZipFile, name: str) -> bytes:
    matches = [entry for entry in archive.infolist() if entry.filename == name]
    if len(matches) != 1:
        _fail(f"Candidate VSIX must contain exactly one `{name}` entry.")
    return archive.read(matches[0])


def _reported_filename(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("Manual real-provider report field `vsix` must identify the candidate file.")
    return value.strip().replace("\\", "/").rsplit("/", 1)[-1]


def validate_candidate_binding(
    report: dict[str, Any],
    candidate_vsix: Path,
    *,
    trusted_public_key_path: Path | None = None,
    channel: str = "stable",
) -> None:
    """Bind completed human evidence to the exact signed target VSIX bytes."""

    candidate = candidate_vsix.expanduser().resolve()
    if not candidate.is_file() or candidate.suffix.casefold() != ".vsix":
        _fail(f"Candidate VSIX does not exist or is not a .vsix file: {candidate}")
    if _reported_filename(report.get("vsix")) != candidate.name:
        _fail("Manual real-provider report names a different VSIX candidate.")

    candidate_bytes = candidate.read_bytes()
    candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
    if report.get("vsix_sha256") != candidate_digest:
        _fail("Manual real-provider report VSIX SHA-256 does not match the candidate bytes.")

    target = str(report.get("platform_target") or "")
    executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
    executable_entry = f"extension/resources/managed-cli/{executable}"
    manifest_entry = "extension/resources/managed-cli/manifest.json"
    public_key_entry = "extension/resources/managed-cli-release-public.pem"
    package_entry = "extension/package.json"
    try:
        with ZipFile(candidate) as archive:
            package_bytes = _read_unique(archive, package_entry)
            vsix_manifest = _read_unique(archive, "extension.vsixmanifest").decode("utf-8")
            managed_manifest_bytes = _read_unique(archive, manifest_entry)
            runtime_bytes = _read_unique(archive, executable_entry)
            public_key_bytes = _read_unique(archive, public_key_entry)
            bundled_runtimes = sorted(
                entry.filename
                for entry in archive.infolist()
                if entry.filename.startswith("extension/resources/managed-cli/alysis-")
            )
    except (BadZipFile, UnicodeDecodeError, OSError) as exc:
        raise ManualReportValidationError(
            "Candidate VSIX is not a readable release package."
        ) from exc

    if bundled_runtimes != [executable_entry]:
        _fail("Candidate VSIX must contain exactly the managed runtime for its reported target.")

    try:
        package = json.loads(package_bytes)
        managed_manifest = json.loads(managed_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManualReportValidationError(
            "Candidate VSIX package or managed manifest is not valid JSON."
        ) from exc
    if not isinstance(package, dict) or not isinstance(managed_manifest, dict):
        _fail("Candidate VSIX package and managed manifest must be JSON objects.")
    from scripts.release.vscode_release_channel import validate_vsix_channel

    try:
        validate_vsix_channel(vsix_manifest, package, channel=channel)
    except ValueError as exc:
        _fail(str(exc))
    if package.get("publisher") != "alysisai" or package.get("name") != "vscode-alysis":
        _fail("Candidate VSIX extension identity is not alysisai.vscode-alysis.")
    if package.get("version") != report.get("extension_version"):
        _fail("Manual real-provider report extension version does not match the candidate VSIX.")
    if managed_manifest.get("schemaVersion") != 3:
        _fail("Candidate VSIX managed runtime manifest schema is unsupported.")
    release = managed_manifest.get("release")
    if not isinstance(release, dict) or release != {
        "sourceCommit": report.get("source_sha"),
        "sourceRepository": report.get("source_repository"),
        "tag": report.get("release_tag"),
    }:
        _fail("Manual report release identity does not match the signed candidate manifest.")
    if managed_manifest.get("artifactVersion") != report.get("managed_artifact_version"):
        _fail("Manual real-provider report artifact version does not match the signed manifest.")

    trusted_key = trusted_public_key_path or (
        PROJECT_ROOT
        / "extensions"
        / "vscode-alysis"
        / "resources"
        / "managed-cli-release-public.pem"
    )
    try:
        trusted_public_key_bytes = trusted_key.read_bytes()
    except OSError as exc:
        raise ManualReportValidationError(
            "Pinned managed-runtime release public key is unavailable."
        ) from exc
    if public_key_bytes != trusted_public_key_bytes:
        _fail("Candidate VSIX managed-runtime public key differs from the pinned release key.")

    compatibility = managed_manifest.get("compatibility")
    extension_compatibility = (
        compatibility.get("extension") if isinstance(compatibility, dict) else None
    )
    expected_extension_version = report.get("extension_version")
    if not isinstance(extension_compatibility, dict) or extension_compatibility != {
        "min": expected_extension_version,
        "max": expected_extension_version,
    }:
        _fail("Signed managed runtime compatibility does not bind the candidate extension version.")

    artifacts = managed_manifest.get("artifacts")
    if not isinstance(artifacts, list):
        _fail("Candidate VSIX managed runtime artifacts are invalid.")
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("target") == target
    ]
    if len(matches) != 1:
        _fail("Signed managed runtime manifest does not contain exactly one reported target.")
    artifact = matches[0]
    runtime_digest = hashlib.sha256(runtime_bytes).hexdigest()
    if (
        artifact.get("executable") != executable
        or artifact.get("sha256") != runtime_digest
        or report.get("managed_cli_sha256") != runtime_digest
    ):
        _fail("Manual report, signed manifest, and packaged managed runtime hashes do not agree.")
    try:
        verify_artifact_signature(managed_manifest, artifact, trusted_public_key_bytes)
    except VsixSbomError as exc:
        raise ManualReportValidationError(
            f"Candidate VSIX managed runtime signature is invalid: {exc}"
        ) from exc


def validate_production_dogfood_binding(
    report: dict[str, Any], production_dogfood_path: Path
) -> None:
    """Bind the manual receipt to exact, valid installed-production evidence bytes."""

    path = production_dogfood_path.expanduser()
    if path.is_symlink():
        _fail("Production dogfood evidence must not be a symlink.")
    try:
        evidence_path = path.resolve(strict=True)
        evidence_bytes = evidence_path.read_bytes()
    except OSError as exc:
        raise ManualReportValidationError("Production dogfood evidence is unavailable.") from exc
    if not evidence_path.is_file() or evidence_path.suffix.casefold() != ".json":
        _fail("Production dogfood evidence must be a regular JSON file.")
    if report.get("production_dogfood_sha256") != hashlib.sha256(evidence_bytes).hexdigest():
        _fail("Manual report does not bind the exact production dogfood evidence bytes.")
    try:
        dogfood = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManualReportValidationError(
            "Production dogfood evidence is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(dogfood, dict):
        _fail("Production dogfood evidence must be a JSON object.")
    try:
        validate_release_valid_summary(dogfood)
        validate_production_vscode_compatibility(dogfood)
    except DogfoodError as exc:
        raise ManualReportValidationError(str(exc)) from exc

    if _reported_filename(report.get("vsix")) != _reported_filename(dogfood.get("vsix")):
        _fail("Manual report and production dogfood name different VSIX candidates.")
    for field in (
        "vsix_sha256",
        "platform_target",
        "release_tag",
        "source_repository",
        "source_sha",
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
            _fail(f"Manual report and production dogfood disagree on {field}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate completed manual real-provider VS Code extension dogfood evidence."
    )
    parser.add_argument(
        "report", help="Local JSON report path. HTTPS URLs are intentionally rejected."
    )
    parser.add_argument(
        "candidate_vsix",
        help="Exact local target-specific VSIX whose bytes were exercised by the report.",
    )
    parser.add_argument(
        "production_dogfood",
        help="Exact local production-dogfood JSON whose bytes the manual report cites.",
    )
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    args = parser.parse_args(argv)
    try:
        report = _load_report(args.report)
        validate_report(report)
        validate_candidate_binding(report, Path(args.candidate_vsix), channel=args.channel)
        validate_production_dogfood_binding(report, Path(args.production_dogfood))
    except ManualReportValidationError as exc:
        print(f"manual real-provider report validation failed: {exc}", file=sys.stderr)
        return 1
    print("manual real-provider report validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
