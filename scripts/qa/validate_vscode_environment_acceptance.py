#!/usr/bin/env python3
"""Create and validate release-bound VS Code environment acceptance evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.validate_vscode_manual_provider_report import (  # noqa: E402
    GITHUB_LOGIN_RE,
    IDENTITY_PLACEHOLDERS,
    SECRET_VALUE_PATTERNS,
)
from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    SUPPORTED_PLATFORM_TARGETS,
)

SCHEMA_NAME = "vscode-environment-acceptance"
SCHEMA_VERSION = 3
REPORT_FILENAME = "vscode-alysis-environment-acceptance.json"
REQUIRED_ENVIRONMENTS = (
    "windows_local",
    "linux_local",
    "macos_local",
    "wsl",
    "remote_ssh",
    "untrusted_workspace",
)
DEFAULT_TARGETS = {
    "windows_local": "win32-x64",
    "linux_local": "linux-x64",
    "macos_local": "darwin-x64",
    "wsl": "linux-x64",
    "remote_ssh": "linux-x64",
    "untrusted_workspace": "linux-x64",
}
COMMON_CHECKS = frozenset(
    {
        "clean_install",
        "bridge_health",
        "normal_chat",
        "run_task",
        "artifacts_diffs",
        "extension_reload_recovery",
        "secret_leak_check",
    }
)
REMOTE_CHECKS = frozenset({"remote_cli_origin", "remote_oauth_fail_closed"})
UNTRUSTED_CHECKS = frozenset(
    {
        "readonly_paths_available",
        "mutating_paths_denied",
        "restricted_settings_ignored",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
CHECK_RECEIPT_KEYS = frozenset({"status", "completed_at", "artifact_sha256", "event_id"})
REPORT_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "status",
        "release_tag",
        "source_sha",
        "candidate_run_id",
        "extension_version",
        "reviewer",
        "started_at",
        "completed_at",
        "environments",
    }
)
ENVIRONMENT_KEYS = frozenset(
    {
        "status",
        "platform_target",
        "vsix",
        "vsix_sha256",
        "managed_artifact_version",
        "managed_cli_sha256",
        "vscode_version",
        "vscode_commit",
        "host_description",
        "host_platform",
        "host_arch",
        "remote_name",
        "workspace_trusted",
        "workspace_scheme",
        "workspace_authority",
        "provider",
        "completed_checks",
    }
)


class EnvironmentAcceptanceError(RuntimeError):
    """Environment acceptance evidence is incomplete, unsafe, or unbound."""


def _fail(message: str) -> None:
    raise EnvironmentAcceptanceError(message)


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentAcceptanceError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        _fail(f"{label} must be a JSON object: {path}")
    return payload


def _require_string(
    payload: dict[str, Any], field: str, *, pattern: re.Pattern[str] | None = None
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string.")
    value = value.strip()
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{field} has an invalid format.")
    return value


def _required_checks(environment: str) -> frozenset[str]:
    required = set(COMMON_CHECKS)
    if environment in {"wsl", "remote_ssh"}:
        required.update(REMOTE_CHECKS)
    if environment == "untrusted_workspace":
        required.update(UNTRUSTED_CHECKS)
    return frozenset(required)


def _parse_utc_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        _fail(f"{field} must be a UTC RFC3339 timestamp ending in Z.")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EnvironmentAcceptanceError(f"{field} must be a real UTC timestamp.") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        _fail(f"{field} must be a UTC RFC3339 timestamp ending in Z.")
    return parsed


def _validate_check_receipt(
    receipt: Any,
    *,
    environment: str,
    check_id: str,
    started_at: dt.datetime,
    completed_at: dt.datetime,
) -> None:
    label = f"environments.{environment}.completed_checks.{check_id}"
    if not isinstance(receipt, dict):
        _fail(f"{label} must be a structured receipt; bare status strings are rejected.")
    unexpected = sorted(set(receipt) - CHECK_RECEIPT_KEYS)
    if unexpected:
        _fail(f"{label} contains unknown fields: {', '.join(unexpected)}")
    if receipt.get("status") != "passed":
        _fail(f"{label}.status must be passed.")
    observed_at = _parse_utc_timestamp(receipt.get("completed_at"), f"{label}.completed_at")
    if observed_at < started_at or observed_at > completed_at:
        _fail(f"{label}.completed_at must fall within the report interval.")
    artifact_sha256 = receipt.get("artifact_sha256")
    event_id = receipt.get("event_id")
    if event_id not in (None, ""):
        _fail(f"{label} must not use a free-form event_id locator.")
    if not isinstance(artifact_sha256, str) or SHA256_RE.fullmatch(artifact_sha256) is None:
        _fail(f"{label}.artifact_sha256 must be lowercase SHA-256.")


def _assert_no_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _assert_no_secrets(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_secrets(nested, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
        _fail(f"Environment acceptance report contains a secret-looking value at {path}.")


def _candidate_evidence(candidate_dir: Path, target: str) -> tuple[str, str, str, str]:
    if target not in SUPPORTED_PLATFORM_TARGETS:
        _fail(f"Unsupported platform target: {target}")
    vsix_name = f"vscode-alysis-{target}.vsix"
    vsix_path = candidate_dir / vsix_name
    if not vsix_path.is_file() or vsix_path.is_symlink():
        _fail(f"Candidate VSIX is missing or unsafe: {vsix_name}")
    dogfood = _json_object(
        candidate_dir / f"production-dogfood-{target}.json",
        "Production dogfood evidence",
    )
    if dogfood.get("platform_target") != target:
        _fail(f"Production dogfood target does not match {target}.")
    digest = hashlib.sha256(vsix_path.read_bytes()).hexdigest()
    if dogfood.get("vsix_sha256") != digest:
        _fail(f"Production dogfood does not bind the exact {target} candidate.")
    artifact_version = dogfood.get("managed_artifact_version")
    managed_digest = dogfood.get("managed_cli_sha256")
    if not isinstance(artifact_version, str) or not artifact_version.strip():
        _fail(f"Production dogfood for {target} has no managed artifact version.")
    if not isinstance(managed_digest, str) or SHA256_RE.fullmatch(managed_digest) is None:
        _fail(f"Production dogfood for {target} has no valid managed runtime digest.")
    return vsix_name, digest, artifact_version, managed_digest


def build_template(
    candidate_dir: Path,
    *,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
) -> dict[str, Any]:
    if re.fullmatch(r"v\d+\.\d+\.\d+", release_tag) is None:
        _fail("release_tag must use vX.Y.Z format.")
    if GIT_OBJECT_ID_RE.fullmatch(source_sha) is None:
        _fail("source_sha must be a lowercase full Git object ID.")
    if candidate_run_id <= 0:
        _fail("candidate_run_id must be positive.")
    version = release_tag.removeprefix("v")
    environments: dict[str, Any] = {}
    for environment in REQUIRED_ENVIRONMENTS:
        target = DEFAULT_TARGETS[environment]
        vsix_name, digest, artifact_version, managed_digest = _candidate_evidence(
            candidate_dir, target
        )
        environments[environment] = {
            "status": "pending",
            "platform_target": target,
            "vsix": vsix_name,
            "vsix_sha256": digest,
            "managed_artifact_version": artifact_version,
            "managed_cli_sha256": managed_digest,
            "vscode_version": "",
            "vscode_commit": "",
            "host_description": "",
            "host_platform": (
                "win32"
                if target.startswith("win32-")
                else "darwin"
                if target.startswith("darwin-")
                else "linux"
            ),
            "host_arch": "arm64" if target.endswith("-arm64") else "x64",
            "remote_name": (
                "wsl"
                if environment == "wsl"
                else "ssh-remote"
                if environment == "remote_ssh"
                else ""
            ),
            "workspace_trusted": environment != "untrusted_workspace",
            "workspace_scheme": (
                "vscode-remote" if environment in {"wsl", "remote_ssh"} else "file"
            ),
            "workspace_authority": (
                "wsl+<distribution>"
                if environment == "wsl"
                else "ssh-remote+<host>"
                if environment == "remote_ssh"
                else ""
            ),
            "provider": "",
            "completed_checks": {
                check_id: {
                    "status": "pending",
                    "completed_at": "",
                    "artifact_sha256": "",
                    "event_id": "",
                }
                for check_id in sorted(_required_checks(environment))
            },
        }
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "status": "manual_steps_written",
        "release_tag": release_tag,
        "source_sha": source_sha,
        "candidate_run_id": candidate_run_id,
        "extension_version": version,
        "reviewer": "",
        "started_at": "",
        "completed_at": "",
        "environments": environments,
    }


def validate_report(
    report: dict[str, Any],
    candidate_dir: Path,
    *,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: int,
) -> None:
    if set(report) != REPORT_KEYS:
        _fail("Environment acceptance report must contain the exact schema-v3 fields.")
    if report.get("schema_name") != SCHEMA_NAME or report.get("schema_version") != SCHEMA_VERSION:
        _fail("Environment acceptance report schema is unsupported.")
    if report.get("status") != "completed":
        _fail("Environment acceptance report status must be completed.")
    if report.get("release_tag") != expected_release_tag:
        _fail("Environment acceptance report release_tag does not match this promotion.")
    if report.get("source_sha") != expected_source_sha:
        _fail("Environment acceptance report source_sha does not match this promotion.")
    if report.get("candidate_run_id") != expected_candidate_run_id:
        _fail("Environment acceptance report candidate_run_id does not match this promotion.")
    expected_version = expected_release_tag.removeprefix("v")
    if (
        report.get("extension_version") != expected_version
        or VERSION_RE.fullmatch(expected_version) is None
    ):
        _fail("Environment acceptance extension_version does not match the release tag.")
    reviewer = _require_string(report, "reviewer")
    if GITHUB_LOGIN_RE.fullmatch(reviewer) is None or reviewer.casefold() in IDENTITY_PLACEHOLDERS:
        _fail("Environment acceptance reviewer must be a non-placeholder GitHub login.")
    started_at = _parse_utc_timestamp(report.get("started_at"), "started_at")
    completed_at = _parse_utc_timestamp(report.get("completed_at"), "completed_at")
    if completed_at < started_at:
        _fail("completed_at must not precede started_at.")

    environments = report.get("environments")
    if not isinstance(environments, dict) or set(environments) != set(REQUIRED_ENVIRONMENTS):
        _fail("Environment acceptance report must contain the exact required environment matrix.")
    for environment in REQUIRED_ENVIRONMENTS:
        entry = environments.get(environment)
        if not isinstance(entry, dict):
            _fail(f"Environment {environment} must be an object.")
        if set(entry) != ENVIRONMENT_KEYS:
            _fail(f"Environment {environment} must contain the exact schema-v3 fields.")
        if entry.get("status") != "passed":
            _fail(f"Environment {environment} status must be passed.")
        target = _require_string(entry, "platform_target")
        if environment == "windows_local" and not target.startswith("win32-"):
            _fail("windows_local must exercise a Windows candidate.")
        if environment == "macos_local" and not target.startswith("darwin-"):
            _fail("macos_local must exercise a macOS candidate.")
        if environment in {"linux_local", "wsl", "remote_ssh"} and not target.startswith("linux-"):
            _fail(f"{environment} must exercise a Linux candidate.")
        vsix_name, digest, artifact_version, managed_digest = _candidate_evidence(
            candidate_dir, target
        )
        if entry.get("vsix") != vsix_name or entry.get("vsix_sha256") != digest:
            _fail(f"Environment {environment} does not bind the exact candidate VSIX.")
        if entry.get("managed_artifact_version") != artifact_version:
            _fail(f"Environment {environment} managed artifact version is inconsistent.")
        if entry.get("managed_cli_sha256") != managed_digest:
            _fail(f"Environment {environment} managed runtime digest is inconsistent.")
        _require_string(entry, "vscode_version", pattern=VERSION_RE)
        _require_string(entry, "vscode_commit", pattern=GIT_OBJECT_ID_RE)
        _require_string(entry, "host_description")
        _require_string(entry, "provider")
        expected_platform = (
            "win32"
            if target.startswith("win32-")
            else "darwin"
            if target.startswith("darwin-")
            else "linux"
        )
        expected_arch = "arm64" if target.endswith("-arm64") else "x64"
        if (
            entry.get("host_platform") != expected_platform
            or entry.get("host_arch") != expected_arch
        ):
            _fail(f"Environment {environment} host OS/architecture does not match its target.")
        expected_remote_name = (
            "wsl" if environment == "wsl" else "ssh-remote" if environment == "remote_ssh" else ""
        )
        if entry.get("remote_name") != expected_remote_name:
            _fail(f"Environment {environment} has an invalid remote_name.")
        expected_trust = environment != "untrusted_workspace"
        if entry.get("workspace_trusted") is not expected_trust:
            _fail(f"Environment {environment} has an invalid Workspace Trust state.")
        expected_scheme = "vscode-remote" if environment in {"wsl", "remote_ssh"} else "file"
        if entry.get("workspace_scheme") != expected_scheme:
            _fail(f"Environment {environment} has an invalid workspace_scheme.")
        authority = entry.get("workspace_authority")
        if environment == "wsl":
            if (
                not isinstance(authority, str)
                or not authority.startswith("wsl+")
                or "<" in authority
            ):
                _fail("Environment wsl must record the concrete WSL workspace authority.")
        elif environment == "remote_ssh":
            if (
                not isinstance(authority, str)
                or not authority.startswith("ssh-remote+")
                or "<" in authority
            ):
                _fail("Environment remote_ssh must record the concrete SSH workspace authority.")
        elif authority != "":
            _fail(f"Environment {environment} local workspace authority must be empty.")
        checks = entry.get("completed_checks")
        required_checks = _required_checks(environment)
        if not isinstance(checks, dict) or set(checks) != set(required_checks):
            _fail(f"Environment {environment} must contain the exact required checks.")
        for check_id in sorted(required_checks):
            _validate_check_receipt(
                checks[check_id],
                environment=environment,
                check_id=check_id,
                started_at=started_at,
                completed_at=completed_at,
            )
    _assert_no_secrets(report)


def validate_file(
    report_path: Path,
    candidate_dir: Path,
    *,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: int,
) -> None:
    validate_report(
        _json_object(report_path, "Environment acceptance report"),
        candidate_dir,
        expected_release_tag=expected_release_tag,
        expected_source_sha=expected_source_sha,
        expected_candidate_run_id=expected_candidate_run_id,
    )


def _identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template_parser = subparsers.add_parser("write-template")
    template_parser.add_argument("candidate_dir", type=Path)
    template_parser.add_argument("output", type=Path)
    _identity_arguments(template_parser)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("report", type=Path)
    validate_parser.add_argument("candidate_dir", type=Path)
    _identity_arguments(validate_parser)
    args = parser.parse_args(argv)
    try:
        if args.command == "write-template":
            payload = build_template(
                args.candidate_dir,
                release_tag=args.release_tag,
                source_sha=args.source_sha,
                candidate_run_id=args.candidate_run_id,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"Wrote incomplete environment acceptance template: {args.output}")
            return 0
        validate_file(
            args.report,
            args.candidate_dir,
            expected_release_tag=args.release_tag,
            expected_source_sha=args.source_sha,
            expected_candidate_run_id=args.candidate_run_id,
        )
    except EnvironmentAcceptanceError as exc:
        print(f"VS Code environment acceptance validation failed: {exc}", file=sys.stderr)
        return 1
    print("VS Code environment acceptance validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
