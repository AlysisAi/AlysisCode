#!/usr/bin/env python3
"""Fail-closed validation for real WSL and Remote-SSH VS Code acceptance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    DogfoodError,
    validate_production_vscode_compatibility,
)

SCHEMA_NAME = "vscode-remote-installed-vsix-acceptance"
SCHEMA_VERSION = 2
TARGET_EXTENSION_ID = "alysisai.vscode-alysis"
REMOTE_DRIVER_ID = "alysisai.alysis-remote-acceptance-driver"
REMOTE_ENVIRONMENTS = {
    "wsl": {
        "remote_name": "wsl",
        "authority_prefix": "wsl+",
        "runner_role": "real-wsl",
        "prerequisite_id": "ms-vscode-remote.remote-wsl",
        "prerequisite_version": "0.88.2",
    },
    "remote_ssh": {
        "remote_name": "ssh-remote",
        "authority_prefix": "ssh-remote+",
        "runner_role": "real-remote-ssh",
        "prerequisite_id": "ms-vscode-remote.remote-ssh",
        "prerequisite_version": "0.112.0",
    },
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
VSCODE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
SAFE_ACCEPTANCE_ID_RE = re.compile(r"[0-9]+-[0-9]+-(?:wsl|remote_ssh)")
RAW_ARTIFACT_RE = re.compile(
    r"vscode-remote-acceptance-raw-(wsl|remote_ssh)-([0-9a-f]{40})-"
    r"([1-9][0-9]*)-attempt-([1-9][0-9]*)"
)
REQUIRED_CHECKS = frozenset(
    {
        "bridge_health",
        "clean_install",
        "extension_reload_recovery",
        "managed_runtime",
        "oauth_fail_closed",
        "remote_identity",
        "secret_leak_check",
        "workspace_trust_enforced",
    }
)
REPORT_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "status",
        "environment",
        "release_tag",
        "source_sha",
        "candidate_run_id",
        "workflow_run_id",
        "workflow_run_attempt",
        "acceptance_id",
        "started_at",
        "completed_at",
        "extension",
        "vscode",
        "prerequisite",
        "host",
        "runtime",
        "reload",
        "checks",
    }
)
EXTENSION_KEYS = frozenset(
    {
        "id",
        "version",
        "mode",
        "vsix_name",
        "vsix_sha256",
        "installed_path_sha256",
    }
)
VSCODE_KEYS = frozenset({"version", "commit", "executable_sha256"})
PREREQUISITE_KEYS = frozenset({"id", "version", "sha256"})
HOST_KEYS = frozenset(
    {
        "platform",
        "arch",
        "remote_name",
        "runner_role",
        "authority_sha256",
        "hostname_sha256",
        "workspace_scheme",
        "workspace_path_sha256",
        "workspace_trusted",
        "workspace_trust_enabled",
        "workspace_trust_grant_method",
        "workspace_trust_initial_state",
        "workspace_trust_parent_grant",
    }
)
RUNTIME_KEYS = frozenset(
    {
        "origin",
        "production",
        "target",
        "artifact_version",
        "cli_version",
        "protocol_version",
        "sha256",
        "release_tag",
        "source_repository",
        "source_sha",
        "signature_verified",
    }
)
RELOAD_KEYS = frozenset({"phases", "profile_state_reused", "runtime_identity_stable"})


class RemoteAcceptanceError(RuntimeError):
    """Remote acceptance inputs or evidence are incomplete or inconsistent."""


def _fail(message: str) -> None:
    raise RemoteAcceptanceError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAcceptanceError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        _fail(f"{label} must be a JSON object: {path}")
    return payload


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail(f"{label} must contain the exact schema-v{SCHEMA_VERSION} fields.")
    return value


def _string(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string.")
    result = value.strip()
    if pattern is not None and pattern.fullmatch(result) is None:
        _fail(f"{label} has an invalid format.")
    return result


def _assert_no_secret_values(value: Any) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_no_secret_values(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_no_secret_values(nested)
        return
    if not isinstance(value, str):
        return
    forbidden = ("api_key", "authorization", "bearer ", "private key", "password=")
    if any(term in value.lower() for term in forbidden):
        _fail("Remote acceptance evidence contains a secret-looking field or value.")


def inspect_extension_vsix(path: Path) -> tuple[str, str, str | None]:
    if not path.is_file() or path.is_symlink():
        _fail(f"VSIX must be a regular, non-symlink file: {path}")
    try:
        with ZipFile(path) as archive:
            package = json.loads(archive.read("extension/package.json"))
            manifest = archive.read("extension.vsixmanifest").decode("utf-8")
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAcceptanceError(f"VSIX is malformed: {path}") from exc
    if not isinstance(package, dict):
        _fail(f"VSIX package metadata must be an object: {path}")
    publisher = _string(package.get("publisher"), "VSIX publisher")
    name = _string(package.get("name"), "VSIX name")
    version = _string(package.get("version"), "VSIX version", VERSION_RE)
    target_match = re.search(r'TargetPlatform="([^"]+)"', manifest)
    return f"{publisher}.{name}", version, target_match.group(1) if target_match else None


def validate_prerequisites(
    *,
    environment: str,
    runner_role: str,
    vscode_executable: Path,
    expected_vscode_sha256: str,
    expected_vscode_version: str,
    remote_extension_vsix: Path,
    expected_remote_extension_sha256: str,
    expected_remote_extension_version: str,
    interactive_session: str,
) -> dict[str, str]:
    spec = REMOTE_ENVIRONMENTS.get(environment)
    if spec is None:
        _fail(f"Unsupported remote environment: {environment}")
    if runner_role != spec["runner_role"]:
        _fail(
            f"Runner role must be {spec['runner_role']} for {environment}; local/hosted substitution is rejected."
        )
    if not interactive_session.strip() or interactive_session.strip().lower() in {
        "services",
        "service",
    }:
        _fail("A real interactive Windows desktop session is required to launch VS Code.")
    if (
        not vscode_executable.is_absolute()
        or not vscode_executable.is_file()
        or vscode_executable.is_symlink()
    ):
        _fail("The pinned VS Code executable must be an existing absolute file.")
    if SHA256_RE.fullmatch(expected_vscode_sha256) is None:
        _fail("The pinned VS Code SHA-256 is missing or invalid.")
    if sha256_file(vscode_executable) != expected_vscode_sha256:
        _fail("The VS Code executable does not match its protected SHA-256 pin.")
    if VERSION_RE.fullmatch(expected_vscode_version) is None:
        _fail("The pinned VS Code version is invalid.")
    completed = subprocess.run(
        [str(vscode_executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        _fail("The pinned VS Code executable failed its version probe.")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != expected_vscode_version:
        _fail("The VS Code executable version does not match the workflow pin.")
    commit = _string(lines[1], "VS Code commit", VSCODE_COMMIT_RE)

    if SHA256_RE.fullmatch(expected_remote_extension_sha256) is None:
        _fail("The Remote Development prerequisite SHA-256 is missing or invalid.")
    if sha256_file(remote_extension_vsix) != expected_remote_extension_sha256:
        _fail("The Remote Development prerequisite does not match its protected SHA-256 pin.")
    extension_id, version, target = inspect_extension_vsix(remote_extension_vsix)
    if extension_id != spec["prerequisite_id"]:
        _fail(f"Unexpected Remote Development prerequisite extension: {extension_id}")
    if version != expected_remote_extension_version:
        _fail("Remote Development prerequisite version does not match the workflow pin.")
    if target not in {None, "win32-x64"}:
        _fail("Remote Development prerequisite must be universal or win32-x64.")
    return {"vscode_commit": commit, "remote_extension_id": extension_id}


def candidate_binding(candidate_dir: Path) -> dict[str, str]:
    vsix = candidate_dir / "vscode-alysis-linux-x64.vsix"
    dogfood_path = candidate_dir / "production-dogfood-linux-x64.json"
    extension_id, version, target = inspect_extension_vsix(vsix)
    if extension_id != TARGET_EXTENSION_ID or target != "linux-x64":
        _fail("Remote acceptance requires the exact linux-x64 production VSIX.")
    dogfood = _json_object(dogfood_path, "Production dogfood evidence")
    try:
        validate_production_vscode_compatibility(dogfood)
    except DogfoodError as exc:
        raise RemoteAcceptanceError(
            f"Remote acceptance requires dual-version production dogfood: {exc}"
        ) from exc
    digest = sha256_file(vsix)
    if (
        dogfood.get("mode") != "installed-production-vsix"
        or dogfood.get("release_valid") is not True
        or dogfood.get("platform_target") != "linux-x64"
        or dogfood.get("vsix_sha256") != digest
    ):
        _fail("Production dogfood does not bind the exact linux-x64 candidate.")
    managed_digest = _string(dogfood.get("managed_cli_sha256"), "managed_cli_sha256", SHA256_RE)
    artifact_version = _string(dogfood.get("managed_artifact_version"), "managed_artifact_version")
    return {
        "vsix_path": str(vsix.resolve()),
        "vsix_name": vsix.name,
        "vsix_sha256": digest,
        "extension_version": version,
        "managed_cli_sha256": managed_digest,
        "managed_artifact_version": artifact_version,
    }


def validate_evidence(
    report: dict[str, Any],
    candidate_dir: Path,
    *,
    expected_environment: str,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: int,
    expected_workflow_run_id: int,
    expected_workflow_run_attempt: int,
    expected_vscode_version: str,
    expected_vscode_commit: str,
    expected_vscode_executable_sha256: str | None = None,
    expected_prerequisite_sha256: str | None = None,
    expected_authority_sha256: str,
    expected_hostname_sha256: str,
    expected_workspace_path_sha256: str,
) -> None:
    spec = REMOTE_ENVIRONMENTS.get(expected_environment)
    if spec is None:
        _fail(f"Unsupported remote environment: {expected_environment}")
    _exact_keys(report, REPORT_KEYS, "Remote acceptance evidence")
    if report.get("schema_name") != SCHEMA_NAME or report.get("schema_version") != SCHEMA_VERSION:
        _fail("Remote acceptance evidence schema is unsupported.")
    if report.get("status") != "passed" or report.get("environment") != expected_environment:
        _fail("Remote acceptance evidence did not pass the expected real environment.")
    if (
        report.get("release_tag") != expected_release_tag
        or TAG_RE.fullmatch(expected_release_tag) is None
    ):
        _fail("Remote acceptance release tag does not match this run.")
    if (
        report.get("source_sha") != expected_source_sha
        or SOURCE_SHA_RE.fullmatch(expected_source_sha) is None
    ):
        _fail("Remote acceptance source SHA does not match this run.")
    for field, expected in (
        ("candidate_run_id", expected_candidate_run_id),
        ("workflow_run_id", expected_workflow_run_id),
        ("workflow_run_attempt", expected_workflow_run_attempt),
    ):
        if not isinstance(expected, int) or expected <= 0 or report.get(field) != expected:
            _fail(f"Remote acceptance {field} does not match this run.")
    acceptance_id = _string(report.get("acceptance_id"), "acceptance_id", SAFE_ACCEPTANCE_ID_RE)
    expected_acceptance_id = (
        f"{expected_workflow_run_id}-{expected_workflow_run_attempt}-{expected_environment}"
    )
    if acceptance_id != expected_acceptance_id:
        _fail("Remote acceptance identity does not match this workflow job.")
    started_at = _string(report.get("started_at"), "started_at", UTC_RE)
    completed_at = _string(report.get("completed_at"), "completed_at", UTC_RE)
    if completed_at < started_at:
        _fail("Remote acceptance completed_at precedes started_at.")

    binding = candidate_binding(candidate_dir)
    if binding["extension_version"] != expected_release_tag.removeprefix("v"):
        _fail("Remote candidate extension version does not match the release tag.")
    extension = _exact_keys(report.get("extension"), EXTENSION_KEYS, "extension")
    if (
        extension.get("id") != TARGET_EXTENSION_ID
        or extension.get("version") != binding["extension_version"]
        or extension.get("mode") != "production"
        or extension.get("vsix_name") != binding["vsix_name"]
        or extension.get("vsix_sha256") != binding["vsix_sha256"]
    ):
        _fail("Remote acceptance extension evidence does not bind the exact candidate.")
    _string(extension.get("installed_path_sha256"), "installed_path_sha256", SHA256_RE)

    vscode = _exact_keys(report.get("vscode"), VSCODE_KEYS, "vscode")
    if (
        vscode.get("version") != expected_vscode_version
        or vscode.get("commit") != expected_vscode_commit
        or SHA256_RE.fullmatch(str(vscode.get("executable_sha256") or "")) is None
        or (
            expected_vscode_executable_sha256 is not None
            and vscode.get("executable_sha256") != expected_vscode_executable_sha256
        )
    ):
        _fail("Remote acceptance VS Code identity does not match the pinned executable.")

    prerequisite = _exact_keys(report.get("prerequisite"), PREREQUISITE_KEYS, "prerequisite")
    if (
        prerequisite.get("id") != spec["prerequisite_id"]
        or prerequisite.get("version") != spec["prerequisite_version"]
        or SHA256_RE.fullmatch(str(prerequisite.get("sha256") or "")) is None
        or (
            expected_prerequisite_sha256 is not None
            and prerequisite.get("sha256") != expected_prerequisite_sha256
        )
    ):
        _fail("Remote prerequisite identity does not match the pinned environment policy.")

    host = _exact_keys(report.get("host"), HOST_KEYS, "host")
    if (
        host.get("platform") != "linux"
        or host.get("arch") != "x64"
        or host.get("remote_name") != spec["remote_name"]
        or host.get("runner_role") != spec["runner_role"]
        or host.get("workspace_scheme") != "vscode-remote"
        or host.get("workspace_trusted") is not True
        or host.get("workspace_trust_enabled") is not True
        or host.get("workspace_trust_grant_method") != "workspace_trust_editor_ctrl_enter"
        or host.get("workspace_trust_initial_state") != "restricted"
        or host.get("workspace_trust_parent_grant") is not False
        or host.get("authority_sha256") != expected_authority_sha256
        or host.get("hostname_sha256") != expected_hostname_sha256
        or host.get("workspace_path_sha256") != expected_workspace_path_sha256
    ):
        _fail(
            "Remote host/workspace identity is invalid or was not produced by a real Linux remote host."
        )
    for field in ("authority_sha256", "hostname_sha256", "workspace_path_sha256"):
        _string(host.get(field), f"host.{field}", SHA256_RE)

    runtime = _exact_keys(report.get("runtime"), RUNTIME_KEYS, "runtime")
    if (
        runtime.get("origin") != "managed"
        or runtime.get("production") is not True
        or runtime.get("target") != "linux-x64"
        or runtime.get("artifact_version") != binding["managed_artifact_version"]
        or runtime.get("sha256") != binding["managed_cli_sha256"]
        or runtime.get("release_tag") != expected_release_tag
        or runtime.get("source_repository") != "https://github.com/AlysisAi/AlysisCode"
        or runtime.get("source_sha") != expected_source_sha
        or runtime.get("signature_verified") is not True
    ):
        _fail("Remote managed runtime evidence is not release-bound and production-valid.")
    _string(runtime.get("cli_version"), "runtime.cli_version", VERSION_RE)
    _string(runtime.get("protocol_version"), "runtime.protocol_version")

    reload_evidence = _exact_keys(report.get("reload"), RELOAD_KEYS, "reload")
    if reload_evidence != {
        "phases": 2,
        "profile_state_reused": True,
        "runtime_identity_stable": True,
    }:
        _fail("Remote Extension Host reload/recovery evidence is incomplete.")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != REQUIRED_CHECKS:
        _fail("Remote acceptance checks are incomplete.")
    if any(value != "passed" for value in checks.values()):
        _fail("Every remote acceptance check must pass.")
    _assert_no_secret_values(report)


def validate_evidence_file(report_path: Path, candidate_dir: Path, **expected: Any) -> None:
    validate_evidence(
        _json_object(report_path, "Remote acceptance evidence"), candidate_dir, **expected
    )


def select_latest_evidence(
    raw_artifacts_dir: Path,
    candidate_dir: Path,
    output_dir: Path,
    *,
    expected_release_tag: str,
    expected_source_sha: str,
    expected_candidate_run_id: int,
    expected_workflow_run_id: int,
    maximum_workflow_run_attempt: int,
    expected_vscode_version: str,
) -> dict[str, int]:
    """Validate every raw artifact and select the newest report per remote environment."""

    if (
        expected_candidate_run_id <= 0
        or expected_workflow_run_id <= 0
        or maximum_workflow_run_attempt <= 0
    ):
        _fail("Remote workflow identities and maximum attempt must be positive.")
    if SOURCE_SHA_RE.fullmatch(expected_source_sha) is None:
        _fail("Remote workflow source SHA is invalid.")
    try:
        artifact_dirs = sorted(raw_artifacts_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise RemoteAcceptanceError(
            f"Raw remote acceptance artifact directory is unavailable: {raw_artifacts_dir}"
        ) from exc
    if not artifact_dirs:
        _fail("No raw remote acceptance artifacts were downloaded.")

    selected: dict[str, tuple[int, Path]] = {}
    for artifact_dir in artifact_dirs:
        if not artifact_dir.is_dir() or artifact_dir.is_symlink():
            _fail("Raw remote acceptance inventory must contain artifact directories only.")
        match = RAW_ARTIFACT_RE.fullmatch(artifact_dir.name)
        if match is None:
            _fail(f"Unexpected raw remote acceptance artifact: {artifact_dir.name}")
        environment, source_sha, candidate_run_id_text, attempt_text = match.groups()
        candidate_run_id = int(candidate_run_id_text)
        attempt = int(attempt_text)
        if source_sha != expected_source_sha or candidate_run_id != expected_candidate_run_id:
            _fail("Raw remote acceptance artifact name is not candidate-bound.")
        if attempt > maximum_workflow_run_attempt:
            _fail("Raw remote acceptance artifact claims a future workflow attempt.")

        expected_filename = f"vscode-remote-acceptance-{environment}.json"
        try:
            entries = list(artifact_dir.iterdir())
        except OSError as exc:
            raise RemoteAcceptanceError(
                f"Raw remote acceptance artifact is unavailable: {artifact_dir}"
            ) from exc
        if (
            len(entries) != 1
            or entries[0].name != expected_filename
            or not entries[0].is_file()
            or entries[0].is_symlink()
        ):
            _fail(f"Raw {environment} artifact must contain only {expected_filename}.")
        report_path = entries[0]
        report = _json_object(report_path, f"{environment} remote acceptance evidence")
        vscode = report.get("vscode")
        host = report.get("host")
        if not isinstance(vscode, dict) or not isinstance(host, dict):
            _fail(f"{environment} remote acceptance identity is incomplete.")
        validate_evidence(
            report,
            candidate_dir,
            expected_environment=environment,
            expected_release_tag=expected_release_tag,
            expected_source_sha=expected_source_sha,
            expected_candidate_run_id=expected_candidate_run_id,
            expected_workflow_run_id=expected_workflow_run_id,
            expected_workflow_run_attempt=attempt,
            expected_vscode_version=expected_vscode_version,
            expected_vscode_commit=vscode.get("commit"),
            expected_authority_sha256=host.get("authority_sha256"),
            expected_hostname_sha256=host.get("hostname_sha256"),
            expected_workspace_path_sha256=host.get("workspace_path_sha256"),
        )
        current = selected.get(environment)
        if current is None or attempt > current[0]:
            selected[environment] = (attempt, report_path)

    if set(selected) != set(REMOTE_ENVIRONMENTS):
        _fail("Raw remote acceptance artifacts must include WSL and Remote-SSH evidence.")
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise RemoteAcceptanceError(
            f"Attested remote acceptance output directory must not already exist: {output_dir}"
        ) from exc
    for environment, (_attempt, report_path) in selected.items():
        shutil.copyfile(report_path, output_dir / f"vscode-remote-acceptance-{environment}.json")
    return {environment: selected[environment][0] for environment in sorted(selected)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    candidate_parser = subparsers.add_parser("candidate-binding")
    candidate_parser.add_argument("candidate_dir", type=Path)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--environment", choices=tuple(REMOTE_ENVIRONMENTS), required=True)
    preflight.add_argument("--runner-role", required=True)
    preflight.add_argument("--vscode-executable", type=Path, required=True)
    preflight.add_argument("--vscode-sha256", required=True)
    preflight.add_argument("--vscode-version", required=True)
    preflight.add_argument("--remote-extension-vsix", type=Path, required=True)
    preflight.add_argument("--remote-extension-sha256", required=True)
    preflight.add_argument("--remote-extension-version", required=True)
    preflight.add_argument("--interactive-session", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("report", type=Path)
    validate.add_argument("candidate_dir", type=Path)
    validate.add_argument("--environment", choices=tuple(REMOTE_ENVIRONMENTS), required=True)
    validate.add_argument("--release-tag", required=True)
    validate.add_argument("--source-sha", required=True)
    validate.add_argument("--candidate-run-id", type=int, required=True)
    validate.add_argument("--workflow-run-id", type=int, required=True)
    validate.add_argument("--workflow-run-attempt", type=int, required=True)
    validate.add_argument("--vscode-version", required=True)
    validate.add_argument("--vscode-commit", required=True)
    validate.add_argument("--authority-sha256", required=True)
    validate.add_argument("--hostname-sha256", required=True)
    validate.add_argument("--workspace-path-sha256", required=True)

    select = subparsers.add_parser("select-latest")
    select.add_argument("raw_artifacts_dir", type=Path)
    select.add_argument("candidate_dir", type=Path)
    select.add_argument("output_dir", type=Path)
    select.add_argument("--release-tag", required=True)
    select.add_argument("--source-sha", required=True)
    select.add_argument("--candidate-run-id", type=int, required=True)
    select.add_argument("--workflow-run-id", type=int, required=True)
    select.add_argument("--maximum-workflow-run-attempt", type=int, required=True)
    select.add_argument("--vscode-version", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "candidate-binding":
            print(json.dumps(candidate_binding(args.candidate_dir), sort_keys=True))
        elif args.command == "preflight":
            print(
                json.dumps(
                    validate_prerequisites(
                        environment=args.environment,
                        runner_role=args.runner_role,
                        vscode_executable=args.vscode_executable,
                        expected_vscode_sha256=args.vscode_sha256,
                        expected_vscode_version=args.vscode_version,
                        remote_extension_vsix=args.remote_extension_vsix,
                        expected_remote_extension_sha256=args.remote_extension_sha256,
                        expected_remote_extension_version=args.remote_extension_version,
                        interactive_session=args.interactive_session,
                    ),
                    sort_keys=True,
                )
            )
        elif args.command == "validate":
            validate_evidence_file(
                args.report,
                args.candidate_dir,
                expected_environment=args.environment,
                expected_release_tag=args.release_tag,
                expected_source_sha=args.source_sha,
                expected_candidate_run_id=args.candidate_run_id,
                expected_workflow_run_id=args.workflow_run_id,
                expected_workflow_run_attempt=args.workflow_run_attempt,
                expected_vscode_version=args.vscode_version,
                expected_vscode_commit=args.vscode_commit,
                expected_authority_sha256=args.authority_sha256,
                expected_hostname_sha256=args.hostname_sha256,
                expected_workspace_path_sha256=args.workspace_path_sha256,
            )
        else:
            print(
                json.dumps(
                    select_latest_evidence(
                        args.raw_artifacts_dir,
                        args.candidate_dir,
                        args.output_dir,
                        expected_release_tag=args.release_tag,
                        expected_source_sha=args.source_sha,
                        expected_candidate_run_id=args.candidate_run_id,
                        expected_workflow_run_id=args.workflow_run_id,
                        maximum_workflow_run_attempt=args.maximum_workflow_run_attempt,
                        expected_vscode_version=args.vscode_version,
                    ),
                    sort_keys=True,
                )
            )
    except RemoteAcceptanceError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
