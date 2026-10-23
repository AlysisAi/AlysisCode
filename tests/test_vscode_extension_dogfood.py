from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from _release_test_helpers import ARTIFACT_VERSION, PACKAGE_VERSION, RELEASE_TAG
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.qa import validate_vscode_manual_provider_report as manual_validator
from scripts.qa import validate_vscode_promotion_bundle as promotion_validator
from scripts.qa import vscode_extension_dogfood as dogfood
from scripts.release.build_managed_cli_manifest import _attestation, _signed_record


def _valid_production_summary() -> dict[str, Any]:
    return {
        "schema_name": "installed-production-vsix",
        "schema_version": 2,
        "mode": "installed-production-vsix",
        "status": "passed",
        "release_valid": True,
        "extension_host": "passed",
        "extension_under_test": "packaged_vsix",
        "vsix": "vscode-alysis-win32-x64.vsix",
        "vsix_sha256": "a" * 64,
        "clean_install": True,
        "restart_check": "passed",
        "restart_profile_reused": True,
        "restart_runtime_identity_check": "passed",
        "extension_host_launches": 2,
        "runtime_origin": "managed",
        "runtime_production": True,
        "cli_path_override": "",
        "native_signature_check": "passed",
        "release_signature_check": "passed",
        "managed_manifest_signature_check": "passed",
        "package_install_check": "passed",
        "bridge_health": "passed",
        "extension_mode": "production",
        "extension_version": "1.2.3",
        "managed_artifact_version": "runtime-1",
        "managed_cli_sha256": "b" * 64,
        "release_tag": RELEASE_TAG,
        "source_repository": "https://github.com/AlysisAi/AlysisCode",
        "source_sha": "1" * 40,
        "platform_target": "win32-x64",
        "vscode_version": "1.109.0",
        "host_platform": "win32",
        "host_arch": "x64",
        "remote_name": "",
        "workspace_trusted": True,
        "workspace_scheme": "file",
        "workspace_authority": "",
        "started_at": "2026-07-30T12:00:00.000Z",
        "completed_at": "2026-07-30T12:00:01.000Z",
    }


def _with_vscode_compatibility(summary: dict[str, Any]) -> dict[str, Any]:
    summary["schema_version"] = 3
    summary["vscode_compatibility"] = {
        "minimum": {
            "requested_version": "1.90.0",
            "version": "1.90.0",
            "commit": "2" * 40,
            "arch": "x64",
            "executable_sha256": "3" * 64,
        },
        "current_stable": {
            "requested_version": "stable",
            "version": "1.130.0",
            "commit": "4" * 40,
            "arch": "x64",
            "executable_sha256": "5" * 64,
        },
    }
    return summary


def _manual_check_receipt(
    check_id: str,
    *,
    status: str = "passed",
    rationale: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "completed_at": "2026-07-29T12:01:00.000Z",
        "artifact_sha256": hashlib.sha256(check_id.encode()).hexdigest(),
        "event_id": "",
        "notes": "",
        "rationale": rationale,
    }


def _completed_manual_provider_report() -> dict[str, Any]:
    return {
        "schema_name": dogfood.MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME,
        "schema_version": dogfood.MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION,
        "mode": "manual-real-provider",
        "status": "completed",
        "provider": "test-provider",
        "extension_version": "1.2.3",
        "vsix": "alysis-win32-x64.vsix",
        "vsix_sha256": "a" * 64,
        "candidate_run_id": 123456,
        "release_tag": RELEASE_TAG,
        "source_repository": "https://github.com/AlysisAi/AlysisCode",
        "source_sha": "1" * 40,
        "production_dogfood_sha256": "c" * 64,
        "platform_target": "win32-x64",
        "vscode_version": "1.109.0",
        "host_platform": "win32",
        "host_arch": "x64",
        "remote_name": "",
        "workspace_trusted": True,
        "workspace_scheme": "file",
        "workspace_authority": "",
        "runtime_origin": "managed",
        "runtime_production": True,
        "managed_artifact_version": "1.2.3",
        "managed_cli_sha256": "b" * 64,
        "release_signature_check": "passed",
        "native_signature_check": "passed",
        "package_install_check": "passed",
        "bridge_health": "passed",
        "cli_path_override": "",
        "completed_checks": {
            check["id"]: _manual_check_receipt(check["id"])
            for check in dogfood.MANUAL_REAL_PROVIDER_REQUIRED_CHECKS
        },
        "known_limitations_confirmed": True,
        "no_p0_p1_blockers": True,
        "secret_leak_check": "passed",
        "reviewer": "release-reviewer",
        "started_at": "2026-07-29T12:00:00.000Z",
        "completed_at": "2026-07-29T13:00:00.000Z",
    }


def _signed_candidate_vsix(
    tmp_path: Path,
    report: dict[str, Any],
    *,
    pre_release: bool = False,
) -> tuple[Path, Path]:
    target = str(report["platform_target"])
    executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
    runtime = b"signed managed CLI fixture"
    runtime_digest = hashlib.sha256(runtime).hexdigest()
    private_key = ec.generate_private_key(ec.SECP256R1())
    release = {
        "sourceCommit": "1" * 40,
        "sourceRepository": "https://github.com/AlysisAi/AlysisCode",
        "tag": RELEASE_TAG,
    }
    compatibility = {
        "extension": {
            "min": report["extension_version"],
            "max": report["extension_version"],
        },
        "protocol": {"min": "1", "max": "1"},
        "cli": {"min": PACKAGE_VERSION, "max": PACKAGE_VERSION},
    }
    provenance = {
        "builderId": (
            "https://github.com/AlysisAi/AlysisCode/.github/workflows/"
            f"managed-cli-vsix-release.yml@refs/tags/{RELEASE_TAG}"
        ),
        "issuer": "https://token.actions.githubusercontent.com",
    }
    unsigned_artifact: dict[str, Any] = {
        "executable": executable,
        "nativeSignature": {
            "evidenceSha256": "4" * 64,
            "policy": "authenticode",
            "signerIdentity": "sha256:" + "2" * 64,
        },
        "sbomSha256": "3" * 64,
        "sha256": runtime_digest,
        "size": len(runtime),
        "target": target,
        "url": (
            f"https://github.com/AlysisAi/AlysisCode/releases/download/{RELEASE_TAG}/{executable}"
        ),
    }
    artifact_version = ARTIFACT_VERSION
    signing_key_id = "test-release-key"
    record = _signed_record(
        release=release,
        artifact_version=artifact_version,
        cli_version=PACKAGE_VERSION,
        compatibility=compatibility,
        signing_key_id=signing_key_id,
        provenance=provenance,
        artifact=unsigned_artifact,
    )
    signature = private_key.sign(_attestation(record), ec.ECDSA(hashes.SHA256()))
    managed_manifest = {
        "schemaVersion": 3,
        "release": release,
        "artifactVersion": artifact_version,
        "cliVersion": PACKAGE_VERSION,
        "compatibility": compatibility,
        "signingKeyId": signing_key_id,
        "provenance": provenance,
        "artifacts": [
            {
                **unsigned_artifact,
                "signature": base64.b64encode(signature).decode("ascii"),
            }
        ],
    }
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    marker = (
        '<Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true" />'
        if pre_release
        else "<Properties />"
    )
    candidate = tmp_path / f"vscode-alysis-{target}.vsix"
    trusted_key = tmp_path / "trusted-managed-cli-release-public.pem"
    trusted_key.write_bytes(public_key)
    with ZipFile(candidate, "w") as archive:
        archive.writestr(
            "extension/package.json",
            json.dumps(
                {
                    "publisher": "alysisai",
                    "name": "vscode-alysis",
                    "version": report["extension_version"],
                }
            ),
        )
        archive.writestr("extension.vsixmanifest", marker)
        archive.writestr(
            "extension/resources/managed-cli/manifest.json",
            json.dumps(managed_manifest, sort_keys=True),
        )
        archive.writestr(f"extension/resources/managed-cli/{executable}", runtime)
        archive.writestr("extension/resources/managed-cli-release-public.pem", public_key)
    report.update(
        {
            "vsix": candidate.name,
            "vsix_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "managed_artifact_version": artifact_version,
            "managed_cli_sha256": runtime_digest,
        }
    )
    return candidate, trusted_key


def test_default_cli_command_prefers_repo_venv_python_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    python = project / ".venv" / ("Scripts/python.exe" if dogfood.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(dogfood, "PROJECT_ROOT", project)

    assert dogfood.default_cli_command() == [
        str(python),
        "-m",
        "alysis_code.cli",
    ]


def test_parse_cli_command_explicit_override_does_not_use_repo_venv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    python = project / ".venv" / ("Scripts/python.exe" if dogfood.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(dogfood, "PROJECT_ROOT", project)

    assert dogfood.parse_cli_command("custom-alysis --flag") == ["custom-alysis", "--flag"]


def test_run_command_replaces_invalid_utf8_output(tmp_path: Path) -> None:
    result = dogfood.run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0x88]))"],
        cwd=tmp_path,
    )

    assert result["stdout"] == "\ufffd"


def _health_payload(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    methods = [
        "initialize",
        "health",
        "getCapabilities",
        "session.create",
        "chat.send",
        "run.start",
        "forge.plan",
        "forge.executePreview",
        "forge.swarm.start",
        "forge.swarm.resume",
        "forge.swarm.list",
        "forge.swarm.status",
        "forge.swarm.result",
        "forge.swarm.cancel",
        "mcp.auth.login.start",
        "mcp.auth.login.status",
        "mcp.auth.login.cancel",
        "browser.start",
        "browser.navigate",
        "browser.snapshot",
        "browser.screenshot",
        "browser.artifact.read",
        "browser.diagnostics",
        "browser.click",
        "browser.type",
        "browser.status",
        "browser.list",
        "browser.close",
        "config.get",
        "doctor.summary",
    ]
    payload: dict[str, Any] = {
        "ok": True,
        "protocol_version": "1",
        "capabilities": {
            "methods": methods,
            "features": {
                "forge": {
                    "swarm": {
                        "supported": True,
                        "cancellation": "cooperative_checkpoint_cancellation",
                        "merge_behavior": "review_only_per_task_apply_discard",
                        "workspace_trust_required": True,
                        "approvals": {"yes_auto_approval": False},
                    },
                    "cancel": {"supported": False},
                    "execute": {
                        "supported_modes": ["review"],
                        "unsafe_modes": {"supported": False},
                    },
                },
                "management": {
                    "methods": {
                        "mcp.auth.login.start": {"supported": True},
                        "mcp.auth.login.status": {"supported": True},
                        "mcp.auth.login.cancel": {"supported": True},
                    },
                    "mcp": {
                        "auth_login": {
                            "supported": True,
                            "flow_state_persistent": True,
                            "loopback_callback_owned": True,
                            "pkce_s256": True,
                            "state_validation": True,
                            "host_header_validation": True,
                            "timeout_expiry": True,
                            "cancel_cleanup": True,
                            "encrypted_token_store": True,
                            "logout_fences_late_token_writes": True,
                            "authorization_code_in_protocol": False,
                            "tokens_in_protocol_params": False,
                        }
                    },
                    "hooks": {"watch": {"supported": False}},
                },
                "resumable_swarm": {
                    "supported": True,
                    "durable": True,
                    "explicit_resume": True,
                    "fresh_permission_fingerprint_required": True,
                    "fenced_worker_leases": True,
                    "restart_recovery": True,
                    "atomic_cancellation": True,
                    "exactly_once_usage_events": True,
                },
                "managed_browser": {
                    "supported": True,
                    "owned_chromium_processes": True,
                    "loopback_cdp_only": True,
                    "private_profiles_outside_workspaces": True,
                    "guarded_navigation": True,
                    "redirect_and_subresource_interception": True,
                    "persistent_child_target_interception": True,
                    "validating_egress_proxy": True,
                    "dns_resolution_pinned_to_numeric_connect": True,
                    "no_direct_network_fallback": True,
                    "loopback_proxy_bypass_removed": True,
                    "non_proxied_udp_disabled": True,
                    "public_destinations_by_default": True,
                    "local_destinations_require_confirmation": True,
                    "bounded_snapshots": True,
                    "chunked_screenshot_artifacts": True,
                    "bounded_diagnostics": True,
                    "session_cleanup": True,
                },
            },
        },
    }
    if extra:
        payload["capabilities"]["features"].update(extra)
    return payload


def test_validate_bridge_health_parses_raw_stdout_larger_than_report_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _health_payload({"large": {"padding": "x" * 25_000}})
    raw = json.dumps(payload)

    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "alysis ide-bridge health",
            "cwd": str(Path.cwd()),
            "exit_code": 0,
            "stdout": raw[:4000] + "\n...[truncated]",
            "stderr": "",
            "stdout_raw_length": len(raw),
            "stdout_truncated": True,
            "_stdout_raw": raw,
            "_stderr_raw": "",
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)

    record = dogfood.validate_bridge_health(["alysis"], {})

    assert record["protocol_version"] == "1"
    assert "session.create" in record["methods"]


def test_validate_bridge_health_fails_clear_on_invalid_raw_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "alysis ide-bridge health",
            "cwd": str(Path.cwd()),
            "exit_code": 0,
            "stdout": '{"ok": true\n...[truncated]',
            "stderr": "",
            "_stdout_raw": '{"ok": true\n...[truncated]',
            "_stderr_raw": "",
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)

    with pytest.raises(dogfood.DogfoodError, match="did not return JSON"):
        dogfood.validate_bridge_health(["alysis"], {})


def test_run_command_report_copy_is_truncated_and_redacted(tmp_path: Path) -> None:
    secret = "sk-test-secret-value-123456"
    record = dogfood.run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('sk-test-secret-value-123456 ' + 'x' * 5000); "
            "sys.stderr.write('Authorization: Bearer abcdefghijklmnop')",
        ],
        cwd=tmp_path,
    )

    assert record["_stdout_raw"].startswith(secret)
    assert record["stdout_truncated"] is True
    assert record["stdout_raw_length"] > len(record["stdout"])
    assert secret not in record["stdout"]
    assert "[REDACTED]" in record["stdout"]
    assert "abcdefghijklmnop" not in record["stderr"]
    assert "[REDACTED]" in record["stderr"]

    report: dict[str, Any] = {"steps": []}
    dogfood.record_step(report, "command", "ok", **record)
    rendered = json.dumps(report, sort_keys=True)

    assert "_stdout_raw" not in rendered
    assert "_stderr_raw" not in rendered
    assert secret not in rendered
    assert "abcdefghijklmnop" not in rendered


def test_validate_bridge_health_rejects_unsafe_advertised_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _health_payload()
    payload["capabilities"]["features"]["forge"]["swarm"]["cancellation"] = "hard"
    raw = json.dumps(payload)

    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "alysis ide-bridge health",
            "cwd": str(Path.cwd()),
            "exit_code": 0,
            "stdout": raw,
            "stderr": "",
            "_stdout_raw": raw,
            "_stderr_raw": "",
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)

    with pytest.raises(dogfood.DogfoodError, match="forge\\.swarm"):
        dogfood.validate_bridge_health(["alysis"], {})


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("hooks.watch.start", "hooks\\.watch lifecycle"),
    ],
)
def test_validate_bridge_health_rejects_weak_lifecycle_method_advertising(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    message: str,
) -> None:
    payload = _health_payload()
    payload["capabilities"]["methods"].append(method)
    raw = json.dumps(payload)

    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "alysis ide-bridge health",
            "cwd": str(Path.cwd()),
            "exit_code": 0,
            "stdout": raw,
            "stderr": "",
            "_stdout_raw": raw,
            "_stderr_raw": "",
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)

    with pytest.raises(dogfood.DogfoodError, match=message):
        dogfood.validate_bridge_health(["alysis"], {})


@pytest.mark.parametrize(
    ("feature", "message"),
    [
        ("mcp_oauth", "MCP OAuth"),
        ("hooks_watch", "hooks\\.watch"),
    ],
)
def test_validate_bridge_health_rejects_weak_lifecycle_feature_support(
    monkeypatch: pytest.MonkeyPatch,
    feature: str,
    message: str,
) -> None:
    payload = _health_payload()
    management = payload["capabilities"]["features"]["management"]
    if feature == "mcp_oauth":
        management["mcp"]["auth_login"] = {"supported": True}
    else:
        management["hooks"]["watch"] = {"supported": True}
    raw = json.dumps(payload)

    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "alysis ide-bridge health",
            "cwd": str(Path.cwd()),
            "exit_code": 0,
            "stdout": raw,
            "stderr": "",
            "_stdout_raw": raw,
            "_stderr_raw": "",
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)

    with pytest.raises(dogfood.DogfoodError, match=message):
        dogfood.validate_bridge_health(["alysis"], {})


def test_write_reports_includes_redacted_summary_artifact(tmp_path: Path) -> None:
    report: dict[str, Any] = {
        "mode": "mock",
        "status": "passed_local_smoke",
        "release_valid": False,
        "release_valid_reason": "Extension Host skipped.",
        "extension_host": "skipped",
        "extension_host_preflight": {"status": "skipped"},
        "api_key_required": False,
        "worktree": str(tmp_path / "fixture"),
        "vsix": "vscode-alysis.vsix",
        "steps": [
            {
                "name": "secret step",
                "status": "ok",
                "stdout": "Authorization: Bearer abcdefghijklmnop",
            }
        ],
        "error": "sk-test-secret-value-123456",
    }

    dogfood.write_reports(tmp_path, report)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    rendered = json.dumps(summary, sort_keys=True)
    assert summary["status"] == "passed_local_smoke"
    assert summary["step_count"] == 1
    assert summary["report_json"].endswith("report.json")
    assert "abcdefghijklmnop" not in rendered
    assert "sk-test-secret-value-123456" not in rendered


def test_skip_host_dogfood_summary_is_not_release_valid() -> None:
    summary = {
        "mode": "mock",
        "status": "passed_local_smoke",
        "release_valid": False,
        "extension_host": "skipped",
    }

    with pytest.raises(dogfood.DogfoodError, match="not release-valid"):
        dogfood.validate_release_valid_summary(summary)


def test_require_package_rejects_vsix_older_than_release_inputs(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    source = extension / "src" / "extension.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export {};\n", encoding="utf-8")
    (extension / "package.json").write_text('{"name":"fixture"}', encoding="utf-8")
    vsix = extension / "fixture.vsix"
    vsix.write_bytes(b"old-vsix")
    dogfood.os.utime(vsix, ns=(1_000_000_000, 1_000_000_000))
    dogfood.os.utime(source, ns=(2_000_000_000, 2_000_000_000))

    with pytest.raises(dogfood.DogfoodError, match="older than extension release inputs"):
        dogfood.ensure_vsix(extension, "require", {})


def test_require_package_records_artifact_and_source_digests(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    source = extension / "src" / "extension.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export {};\n", encoding="utf-8")
    (extension / "package.json").write_text('{"name":"fixture"}', encoding="utf-8")
    vsix = extension / "fixture.vsix"
    vsix.write_bytes(b"fresh-vsix")
    future = max(path.stat().st_mtime_ns for path in (source, extension / "package.json")) + 1
    dogfood.os.utime(vsix, ns=(future, future))

    selected, record = dogfood.ensure_vsix(extension, "require", {})

    assert selected == vsix
    assert record["vsix_sha256"] == dogfood.hashlib.sha256(b"fresh-vsix").hexdigest()
    assert len(record["source_snapshot_sha256"]) == 64


def test_explicit_target_vsix_is_selected_by_exact_path_not_directory_mtime(
    tmp_path: Path,
) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    newer_generic = extension / "generic.vsix"
    newer_generic.write_bytes(b"generic")
    target = tmp_path / "retained" / "vscode-alysis-linux-x64.vsix"
    target.parent.mkdir()
    target.write_bytes(b"target-specific")

    selected, record = dogfood.ensure_vsix(
        extension,
        "require",
        {},
        explicit_vsix=target,
    )

    assert selected == target.resolve()
    assert record["source"] == "explicit"
    assert record["vsix_sha256"] == dogfood.hashlib.sha256(b"target-specific").hexdigest()


def test_manual_mode_requires_exact_target_vsix_argument(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        dogfood.main(
            [
                "--mode",
                "manual-real-provider",
                "--report-root",
                str(tmp_path),
            ]
        )


def test_manual_checklist_environment_does_not_inherit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM", "signing-secret")

    environment = dogfood.manual_dogfood_env()

    assert environment["PATH"] == "safe-path"
    assert "OPENAI_API_KEY" not in environment
    assert "GH_TOKEN" not in environment
    assert "ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == dogfood.os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_valid_extension_host_summary_is_release_valid() -> None:
    dogfood.validate_release_valid_summary(_valid_production_summary())


def test_production_compatibility_requires_minimum_and_current_stable_identity() -> None:
    summary = _with_vscode_compatibility(_valid_production_summary())
    dogfood.validate_production_vscode_compatibility(summary)


@pytest.mark.parametrize(
    ("row", "field", "value"),
    (
        ("minimum", "version", "1.89.0"),
        ("minimum", "requested_version", "stable"),
        ("current_stable", "requested_version", "1.130.0"),
        ("current_stable", "version", "1.80.0"),
        ("current_stable", "commit", "mutable"),
        ("current_stable", "executable_sha256", "missing"),
        ("current_stable", "arch", "arm64"),
    ),
)
def test_production_compatibility_rejects_unbound_or_wrong_host_identity(
    row: str, field: str, value: object
) -> None:
    summary = _with_vscode_compatibility(_valid_production_summary())
    summary["vscode_compatibility"][row][field] = value
    with pytest.raises(dogfood.DogfoodError):
        dogfood.validate_production_vscode_compatibility(summary)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("restart_check", "skipped", "restart_check"),
        ("restart_profile_reused", False, "restart_profile_reused"),
        ("restart_runtime_identity_check", "failed", "restart_runtime_identity_check"),
        ("extension_host_launches", 1, "extension_host_launches"),
    ],
)
def test_release_summary_requires_two_phase_restart_evidence(
    field: str, value: object, message: str
) -> None:
    summary = _valid_production_summary()
    summary[field] = value

    with pytest.raises(dogfood.DogfoodError, match=message):
        dogfood.validate_release_valid_summary(summary)


def test_mock_summary_cannot_claim_release_valid() -> None:
    summary = _valid_production_summary()
    summary["mode"] = "mock"

    with pytest.raises(dogfood.DogfoodError, match="mode must be installed-production-vsix"):
        dogfood.validate_release_valid_summary(summary)


def test_component_summary_cannot_be_mislabeled_as_release_valid() -> None:
    component = {
        "status": "passed",
        "component_valid": True,
        "release_valid": False,
        "extension_host": "passed",
        "extension_under_test": "packaged_vsix",
        "vsix_sha256": "b" * 64,
    }

    dogfood.validate_component_valid_summary(component)
    with pytest.raises(dogfood.DogfoodError, match="not release-valid"):
        dogfood.validate_release_valid_summary(component)


def test_source_extension_host_summary_is_not_release_valid() -> None:
    with pytest.raises(dogfood.DogfoodError, match="packaged_vsix"):
        dogfood.validate_release_valid_summary(
            {
                "mode": "mock",
                "status": "passed",
                "release_valid": True,
                "extension_host": "passed",
                "extension_under_test": "source",
                "vsix": None,
                "vsix_sha256": None,
            }
        )


def test_require_cached_without_vscode_executable_path_fails_clear() -> None:
    preflight = dogfood.extension_host_preflight("require-cached", env={})

    assert preflight["status"] == "failed"
    assert preflight["release_valid"] is False
    assert "VSCODE_TEST_EXECUTABLE_PATH" in str(preflight["message"])
    assert "--vscode-executable" in str(preflight["message"])


def test_missing_cached_vscode_executable_fails_with_remediation(tmp_path: Path) -> None:
    preflight = dogfood.extension_host_preflight(
        "require-cached",
        vscode_executable=tmp_path / "missing-code",
        env={},
    )

    assert preflight["status"] == "failed"
    assert "missing or not executable" in str(preflight["message"])
    assert "VSCODE_TEST_EXECUTABLE_PATH" in str(preflight["message"])


def test_run_mode_download_unavailable_explains_cached_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dogfood,
        "check_vscode_download_available",
        lambda: (False, "update.code.visualstudio.com:443 unavailable: offline"),
    )

    preflight = dogfood.extension_host_preflight("run", env={})

    assert preflight["status"] == "failed"
    assert "download failed/unavailable" in str(preflight["message"])
    assert "VSCODE_TEST_EXECUTABLE_PATH" in str(preflight["message"])


def test_extension_host_failure_message_and_report_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "sk-test-secret-value-123456"

    def fake_run_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "command": "npm run test:integration",
            "cwd": str(tmp_path),
            "exit_code": 1,
            "stdout": secret + " " + "x" * 5000,
            "stderr": "Authorization: Bearer abcdefghijklmnop",
            "stdout_raw_length": 5030,
            "stderr_raw_length": 37,
            "stdout_truncated": True,
            "stderr_truncated": False,
        }

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)
    monkeypatch.setattr(dogfood, "require_executable", lambda name: f"/resolved/{name}")
    preflight = {
        "status": "passed",
        "mode": "run",
        "release_valid": True,
        "vscode_executable": None,
        "vscode_executable_source": "download",
    }

    with pytest.raises(dogfood.ExtensionHostDogfoodError) as exc_info:
        dogfood.run_extension_host(tmp_path, {}, "run", preflight=preflight)

    message = str(exc_info.value)
    assert "Extension Host integration test failed" in message
    assert "[REDACTED]" in message
    assert secret not in message
    assert "abcdefghijklmnop" not in message
    assert len(message) < 1500

    report = {"steps": []}
    dogfood.record_named_result(report, "cockpit mock extension host", exc_info.value.record)
    rendered = json.dumps(report, sort_keys=True)
    assert secret not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "[REDACTED]" in rendered


def test_extension_host_resolves_npm_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    monkeypatch.setattr(
        dogfood,
        "require_executable",
        lambda name: "C:/Program Files/nodejs/npm.CMD" if name == "npm" else name,
    )

    def fake_run_command(argv: list[str], **_kwargs: Any) -> dict[str, Any]:
        seen.extend(argv)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)
    preflight = {
        "status": "passed",
        "mode": "require-cached",
        "release_valid": True,
        "vscode_executable": None,
        "vscode_executable_source": "download",
    }

    result = dogfood.run_extension_host(
        tmp_path,
        {},
        "require-cached",
        preflight=preflight,
    )

    assert seen[:3] == ["C:/Program Files/nodejs/npm.CMD", "run", "test:integration"]
    assert result["extension_host"] == "passed"


@pytest.mark.parametrize("warning", ["[DEP0190]", "shell option true"])
def test_extension_host_rejects_unsafe_shell_launch_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    warning: str,
) -> None:
    monkeypatch.setattr(dogfood, "require_executable", lambda _name: "npm")
    monkeypatch.setattr(
        dogfood,
        "run_command",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "stdout": "24 passing",
            "stderr": warning,
        },
    )
    preflight = {
        "status": "passed",
        "mode": "require-cached",
        "release_valid": True,
        "vscode_executable": None,
        "vscode_executable_source": "download",
    }

    with pytest.raises(dogfood.ExtensionHostDogfoodError, match="unsafe shell-based"):
        dogfood.run_extension_host(tmp_path, {}, "require-cached", preflight=preflight)


def test_extension_host_runs_against_the_unpacked_vsix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vsix = tmp_path / "alysis.vsix"
    with dogfood.zipfile.ZipFile(vsix, "w") as archive:
        archive.writestr("extension/package.json", '{"main":"./out/src/extension.js"}')
        archive.writestr("extension/out/src/extension.js", "module.exports = {};")
    observed: dict[str, Any] = {}
    monkeypatch.setattr(dogfood, "require_executable", lambda name: f"resolved-{name}")

    def fake_run_command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        extension_path = Path(kwargs["env"]["ALYSIS_TEST_EXTENSION_PATH"])
        observed["argv"] = argv
        observed["packaged_main_present"] = (
            extension_path / "out" / "src" / "extension.js"
        ).is_file()
        observed["source_path_reused"] = extension_path == tmp_path.resolve()
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(dogfood, "run_command", fake_run_command)
    preflight = {
        "status": "passed",
        "mode": "require-cached",
        "release_valid": True,
        "vscode_executable": None,
        "vscode_executable_source": "download",
    }

    result = dogfood.run_extension_host(
        tmp_path,
        {},
        "require-cached",
        vsix=vsix,
        preflight=preflight,
    )

    assert observed == {
        "argv": ["resolved-npm", "run", "test:integration"],
        "packaged_main_present": True,
        "source_path_reused": False,
    }
    assert result["extension_under_test"] == "packaged_vsix"
    assert result["vsix"] == str(vsix.resolve())


@pytest.mark.parametrize("archive_path", ["../outside", "..\\outside", "C:/outside"])
def test_unpacking_vsix_rejects_archive_path_escape(
    tmp_path: Path,
    archive_path: str,
) -> None:
    vsix = tmp_path / "unsafe.vsix"
    with dogfood.zipfile.ZipFile(vsix, "w") as archive:
        archive.writestr(archive_path, "unsafe")

    with pytest.raises(dogfood.DogfoodError, match="unsafe archive path"):
        with dogfood.unpacked_vsix_extension(vsix):
            pass


@pytest.mark.parametrize("main", ["../outside.js", "..\\outside.js", "C:/outside.js"])
def test_unpacking_vsix_rejects_unsafe_main_entry_point(
    tmp_path: Path,
    main: str,
) -> None:
    vsix = tmp_path / "unsafe-main.vsix"
    with dogfood.zipfile.ZipFile(vsix, "w") as archive:
        archive.writestr("extension/package.json", json.dumps({"main": main}))

    with pytest.raises(dogfood.DogfoodError, match="main entry point is unsafe"):
        with dogfood.unpacked_vsix_extension(vsix):
            pass


def test_cached_extension_host_failure_is_not_mislabeled_as_download_failure() -> None:
    message = dogfood.summarize_extension_host_failure(
        {
            "exit_code": 1,
            "stderr": "Activation failed after loading cached download archive.",
            "stdout": "Cannot find module './backend/ActionResultStore'",
            "preflight": {
                "status": "passed",
                "download_required": False,
                "vscode_executable_source": "--vscode-executable",
            },
        }
    )

    assert "Extension Host integration test failed" in message
    assert "Cannot find module" in message
    assert dogfood.EXTENSION_HOST_DOWNLOAD_FAILURE not in message


def test_normal_ci_runs_real_extension_host_on_all_desktop_platforms() -> None:
    workflow = (dogfood.PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "VS Code extension (${{ matrix.os }})" in workflow
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert "macos-latest" in workflow
    assert "xvfb-run -a npm run test:integration" in workflow
    assert "runner.os != 'Linux'" in workflow
    assert workflow.count("npm run test:integration") >= 2
    assert "npm run package:dev-vsix -- --pre-release" in workflow
    assert workflow.count("vscode_extension_dogfood.py --mode mock --package-mode require") == 2
    assert workflow.count("Dogfood unpacked VSIX") == 2
    assert "npm audit --audit-level=high" in workflow


def test_manual_real_provider_report_has_stable_required_checks(tmp_path: Path) -> None:
    required = dogfood.manual_required_checks()
    required_ids = {check["id"] for check in required}

    assert "managed_runtime_clean_install" in required_ids
    assert "configure_provider_secret_storage" in required_ids
    assert "normal_chat" in required_ids
    assert "run_task" in required_ids
    assert "model_info" in required_ids
    assert "subagent_status_on_off" in required_ids
    assert "image_path" in required_ids
    assert "paste_image" in required_ids
    assert "trace_lifecycle" in required_ids
    assert "terminals_lifecycle" in required_ids
    assert "slash_plan" in required_ids
    assert "forge_show_status" in required_ids
    assert "assistant_show_update" in required_ids
    assert "goal_show_update" in required_ids
    assert "task_show_update" in required_ids
    assert "execute_preview" in required_ids
    assert "execute_plan_review_mode" in required_ids
    assert "diffs_artifacts" in required_ids
    assert "manage_tree_domains" in required_ids
    assert "forge_exec_unsupported_warning" in required_ids
    assert "forge_plan_regenerate" in required_ids
    assert "mcp_oauth_local_lifecycle" in required_ids
    assert "hooks_watch_not_advertised" in required_ids
    assert "active_cancellation_cooperative" in required_ids
    assert "secret_redaction" in required_ids
    assert "missing_provider_recovery" in required_ids
    assert "broken_cli_recovery" in required_ids

    report: dict[str, Any] = {
        "mode": "manual-real-provider",
        "status": "manual_steps_written",
        "release_valid": False,
        "release_valid_reason": "Manual steps only.",
        "extension_host": "skipped",
        "extension_host_preflight": {"status": "skipped"},
        "api_key_required": True,
        "worktree": str(tmp_path / "fixture"),
        "vsix": "vscode-alysis.vsix",
        "steps": [],
        "manual_steps": dogfood.manual_steps(),
        "required_checks": required,
    }

    dogfood.write_reports(tmp_path, report)

    checklist = (tmp_path / "manual_checklist.md").read_text(encoding="utf-8")
    template = json.loads((tmp_path / "manual_report_template.json").read_text(encoding="utf-8"))

    assert "`managed_runtime_clean_install`" in checklist
    assert "`normal_chat`" in checklist
    assert "`run_task`" in checklist
    assert "`forge_show_status`" in checklist
    assert "`trace_lifecycle`" in checklist
    assert "manual_steps_written" in checklist
    assert "<production-dogfood.json>" in checklist
    assert template["schema_name"] == dogfood.MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME
    assert template["schema_version"] == dogfood.MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION
    assert template["status"] == "draft"
    assert template["runtime_origin"] == ""
    assert template["runtime_production"] is False
    assert template["release_signature_check"] == "pending"
    assert template["bridge_health"] == "pending"
    assert template["cli_path_override"] == ""
    assert template["candidate_run_id"] is None
    assert template["release_tag"] == ""
    assert template["source_repository"] == "https://github.com/AlysisAi/AlysisCode"
    assert template["source_sha"] == ""
    assert template["production_dogfood_sha256"] == ""
    assert template["workspace_trusted"] is None
    assert template["started_at"] == ""
    assert set(template["completed_checks"]) == required_ids
    assert all(
        set(receipt)
        == {
            "status",
            "completed_at",
            "artifact_sha256",
            "event_id",
            "notes",
            "rationale",
        }
        for receipt in template["completed_checks"].values()
    )


def test_completed_manual_provider_report_binds_managed_runtime_evidence() -> None:
    report = _completed_manual_provider_report()

    manual_validator.validate_report(report)


def test_manual_provider_check_waiver_policy_partitions_every_required_check() -> None:
    required_ids = {check["id"] for check in dogfood.MANUAL_REAL_PROVIDER_REQUIRED_CHECKS}

    assert not (manual_validator.NON_WAIVABLE_CHECK_IDS & manual_validator.WAIVABLE_CHECK_IDS)
    assert (
        manual_validator.NON_WAIVABLE_CHECK_IDS | manual_validator.WAIVABLE_CHECK_IDS
    ) == required_ids
    assert manual_validator.WAIVABLE_CHECK_IDS == {
        "mcp_oauth_local_lifecycle",
        "run_swarm_capability_gate",
    }


def test_manual_provider_report_rejects_all_checks_not_applicable() -> None:
    report = _completed_manual_provider_report()
    report["completed_checks"] = {
        check["id"]: _manual_check_receipt(
            check["id"],
            status="not_applicable",
            rationale="Alternate test shape was unavailable for this candidate.",
        )
        for check in dogfood.MANUAL_REAL_PROVIDER_REQUIRED_CHECKS
    }

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="Non-waivable required checks must be `passed`",
    ):
        manual_validator.validate_report(report)


@pytest.mark.parametrize("check_id", sorted(manual_validator.WAIVABLE_CHECK_IDS))
def test_manual_provider_report_accepts_explicit_waiver_with_rationale(
    check_id: str,
) -> None:
    report = _completed_manual_provider_report()
    report["completed_checks"][check_id] = _manual_check_receipt(
        check_id,
        status="not_applicable",
        rationale="The alternate capability or deployment shape was unavailable.",
    )

    manual_validator.validate_report(report)


def test_manual_provider_report_rejects_waiver_without_rationale() -> None:
    report = _completed_manual_provider_report()
    check_id = next(iter(manual_validator.WAIVABLE_CHECK_IDS))
    report["completed_checks"][check_id] = _manual_check_receipt(check_id, status="not_applicable")

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="explicitly waivable checks",
    ):
        manual_validator.validate_report(report)


def test_manual_provider_report_rejects_unknown_check_id() -> None:
    report = _completed_manual_provider_report()
    report["completed_checks"]["fabricated_check"] = {"status": "passed"}

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="contains unknown checks: fabricated_check",
    ):
        manual_validator.validate_report(report)


def test_manual_provider_report_rejects_bare_status_receipts() -> None:
    report = _completed_manual_provider_report()
    check_id = next(iter(report["completed_checks"]))
    report["completed_checks"][check_id] = "passed"

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="structured receipt object; bare status strings",
    ):
        manual_validator.validate_report(report)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-29T12:00:00+00:00",
        "2026-07-29 12:00:00Z",
        "2026-02-30T12:00:00Z",
        "2026-07-29T12:00Z",
        "2026-07-29T12:00:00z",
    ],
)
def test_manual_provider_report_rejects_noncanonical_or_impossible_timestamps(
    timestamp: str,
) -> None:
    report = _completed_manual_provider_report()
    report["started_at"] = timestamp

    with pytest.raises(manual_validator.ManualReportValidationError, match="RFC3339"):
        manual_validator.validate_report(report)


def test_manual_provider_report_rejects_receipt_outside_report_interval() -> None:
    report = _completed_manual_provider_report()
    check_id = next(iter(report["completed_checks"]))
    report["completed_checks"][check_id]["completed_at"] = "2026-07-29T14:00:00Z"

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="must fall within the report interval",
    ):
        manual_validator.validate_report(report)


@pytest.mark.parametrize(
    ("artifact_sha256", "event_id", "message"),
    [
        ("", "", "immutable artifact_sha256"),
        ("a" * 64, "event/one", "immutable artifact_sha256"),
        ("short", "", "artifact_sha256"),
        ("", "event id with spaces", "immutable artifact_sha256"),
    ],
)
def test_manual_provider_report_requires_one_valid_receipt_locator(
    artifact_sha256: str,
    event_id: str,
    message: str,
) -> None:
    report = _completed_manual_provider_report()
    check_id = next(iter(report["completed_checks"]))
    report["completed_checks"][check_id]["artifact_sha256"] = artifact_sha256
    report["completed_checks"][check_id]["event_id"] = event_id

    with pytest.raises(manual_validator.ManualReportValidationError, match=message):
        manual_validator.validate_report(report)


def test_manual_provider_report_accepts_artifact_hash_receipt() -> None:
    report = _completed_manual_provider_report()
    check_id = next(iter(report["completed_checks"]))
    report["completed_checks"][check_id]["artifact_sha256"] = "d" * 64
    report["completed_checks"][check_id]["event_id"] = ""

    manual_validator.validate_report(report)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("runtime_origin", "override", "runtime_origin"),
        ("runtime_production", False, "runtime_production"),
        ("cli_path_override", "C:/dev/alysis.exe", "cli_path_override"),
        ("release_signature_check", "skipped", "release_signature_check"),
        ("native_signature_check", "skipped", "native_signature_check"),
        ("package_install_check", "skipped", "package_install_check"),
        ("bridge_health", "skipped", "bridge_health"),
        ("vsix_sha256", "short", "vsix_sha256"),
        ("managed_cli_sha256", "short", "managed_cli_sha256"),
        ("platform_target", "generic", "platform_target"),
        ("candidate_run_id", 0, "candidate_run_id"),
        ("release_tag", "main", "release_tag"),
        ("source_repository", "https://example.test/fork", "source_repository"),
        ("source_sha", "A" * 40, "source_sha"),
        ("production_dogfood_sha256", "short", "production_dogfood_sha256"),
        ("reviewer", "TBD", "reviewer"),
        ("vscode_version", "stable", "vscode_version"),
        ("host_platform", "freebsd", "host_platform"),
        ("host_arch", "ia32", "host_arch"),
        ("workspace_trusted", False, "workspace_trusted"),
        ("workspace_scheme", "vscode-remote", "workspace_scheme"),
    ],
)
def test_manual_provider_report_rejects_unproven_runtime(
    field: str,
    value: object,
    message: str,
) -> None:
    report = _completed_manual_provider_report()
    report[field] = value

    with pytest.raises(manual_validator.ManualReportValidationError, match=message):
        manual_validator.validate_report(report)


def test_manual_provider_report_is_bound_to_exact_signed_candidate(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)

    manual_validator.validate_report(report)
    manual_validator.validate_candidate_binding(
        report,
        candidate,
        trusted_public_key_path=trusted_key,
    )


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_marketplace_promotion_target_binds_manual_automated_and_sbom_evidence(
    tmp_path: Path,
    channel: str,
) -> None:
    report = _completed_manual_provider_report()
    if channel == "beta":
        report["extension_version"] = "0.3.0"
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report, pre_release=channel == "beta")
    report_path = tmp_path / "manual.json"
    dogfood_path = tmp_path / "dogfood.json"
    dogfood_bytes = json.dumps(_release_valid_dogfood(report, candidate)).encode("utf-8")
    dogfood_path.write_bytes(dogfood_bytes)
    report["production_dogfood_sha256"] = hashlib.sha256(dogfood_bytes).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sbom_path = tmp_path / "candidate.cdx.json"
    sbom_path.write_text(json.dumps(_candidate_sbom(report, candidate)), encoding="utf-8")

    manual_validator.validate_production_dogfood_binding(report, dogfood_path)
    promotion_validator.validate_target_bundle(
        channel=channel,
        target="win32-x64",
        candidate_vsix=candidate,
        sbom_path=sbom_path,
        dogfood_path=dogfood_path,
        report_path=report_path,
        release_tag=report["release_tag"],
        source_sha=report["source_sha"],
        candidate_run_id=report["candidate_run_id"],
        trusted_public_key_path=trusted_key,
    )
    with pytest.raises(
        promotion_validator.PromotionBundleValidationError,
        match="exact candidate run and release source",
    ):
        promotion_validator.validate_target_bundle(
            channel=channel,
            target="win32-x64",
            candidate_vsix=candidate,
            sbom_path=sbom_path,
            dogfood_path=dogfood_path,
            report_path=report_path,
            release_tag=report["release_tag"],
            source_sha=report["source_sha"],
            candidate_run_id=report["candidate_run_id"] + 1,
            trusted_public_key_path=trusted_key,
        )


def test_marketplace_promotion_rejects_dogfood_for_different_candidate(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    report_path = tmp_path / "manual.json"
    dogfood = _release_valid_dogfood(report, candidate)
    dogfood["vsix_sha256"] = "f" * 64
    dogfood_path = tmp_path / "dogfood.json"
    dogfood_bytes = json.dumps(dogfood).encode("utf-8")
    dogfood_path.write_bytes(dogfood_bytes)
    report["production_dogfood_sha256"] = hashlib.sha256(dogfood_bytes).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sbom_path = tmp_path / "candidate.cdx.json"
    sbom_path.write_text(json.dumps(_candidate_sbom(report, candidate)), encoding="utf-8")

    with pytest.raises(
        promotion_validator.PromotionBundleValidationError,
        match="VSIX SHA-256",
    ):
        promotion_validator.validate_target_bundle(
            target="win32-x64",
            candidate_vsix=candidate,
            sbom_path=sbom_path,
            dogfood_path=dogfood_path,
            report_path=report_path,
            release_tag=report["release_tag"],
            source_sha=report["source_sha"],
            candidate_run_id=report["candidate_run_id"],
            trusted_public_key_path=trusted_key,
        )


def test_marketplace_promotion_rejects_unbound_production_dogfood_bytes(
    tmp_path: Path,
) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    dogfood_path = tmp_path / "dogfood.json"
    dogfood_path.write_text(json.dumps(_release_valid_dogfood(report, candidate)), encoding="utf-8")
    report["production_dogfood_sha256"] = "e" * 64
    report_path = tmp_path / "manual.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sbom_path = tmp_path / "candidate.cdx.json"
    sbom_path.write_text(json.dumps(_candidate_sbom(report, candidate)), encoding="utf-8")

    with pytest.raises(
        manual_validator.ManualReportValidationError,
        match="exact production dogfood evidence bytes",
    ):
        manual_validator.validate_production_dogfood_binding(report, dogfood_path)
    with pytest.raises(
        promotion_validator.PromotionBundleValidationError,
        match="exact production dogfood report bytes",
    ):
        promotion_validator.validate_target_bundle(
            target="win32-x64",
            candidate_vsix=candidate,
            sbom_path=sbom_path,
            dogfood_path=dogfood_path,
            report_path=report_path,
            release_tag=report["release_tag"],
            source_sha=report["source_sha"],
            candidate_run_id=report["candidate_run_id"],
            trusted_public_key_path=trusted_key,
        )


def test_marketplace_promotion_rejects_manual_host_metadata_disagreement(
    tmp_path: Path,
) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    dogfood_path = tmp_path / "dogfood.json"
    dogfood_bytes = json.dumps(_release_valid_dogfood(report, candidate)).encode("utf-8")
    dogfood_path.write_bytes(dogfood_bytes)
    report["production_dogfood_sha256"] = hashlib.sha256(dogfood_bytes).hexdigest()
    report["vscode_version"] = "1.108.0"
    report_path = tmp_path / "manual.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sbom_path = tmp_path / "candidate.cdx.json"
    sbom_path.write_text(json.dumps(_candidate_sbom(report, candidate)), encoding="utf-8")

    with pytest.raises(
        promotion_validator.PromotionBundleValidationError,
        match="disagree on vscode_version",
    ):
        promotion_validator.validate_target_bundle(
            target="win32-x64",
            candidate_vsix=candidate,
            sbom_path=sbom_path,
            dogfood_path=dogfood_path,
            report_path=report_path,
            release_tag=report["release_tag"],
            source_sha=report["source_sha"],
            candidate_run_id=report["candidate_run_id"],
            trusted_public_key_path=trusted_key,
        )


def _release_valid_dogfood(report: dict[str, Any], candidate: Path) -> dict[str, Any]:
    summary = _valid_production_summary()
    summary.update(
        {
            "extension_version": report["extension_version"],
            "vsix": candidate.name,
            "vsix_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "managed_artifact_version": report["managed_artifact_version"],
            "managed_cli_sha256": report["managed_cli_sha256"],
            "platform_target": report["platform_target"],
        }
    )
    return _with_vscode_compatibility(summary)


@pytest.mark.parametrize("channel", ["stable", "beta"])
@pytest.mark.parametrize(
    "workflow_name",
    [
        "managed-cli-vsix-release.yml",
        "vscode-installed-live-provider-qa.yml",
        "vscode-extension-untrusted-workspace.yml",
    ],
)
def test_workflow_inline_dogfood_validation_accepts_selected_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    workflow_name: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github" / "workflows" / workflow_name).read_text(
        encoding="utf-8"
    )
    embedded = [
        textwrap.dedent(block)
        for block in re.findall(r"python - <<'PY'\n(.*?)\n          PY", workflow, re.DOTALL)
        if "validate_production_dogfood_candidate_binding(" in block
    ]
    assert len(embedded) == 1

    target = "linux-x64"
    managed_workflow = workflow_name == "managed-cli-vsix-release.yml"
    candidate_dir = tmp_path / ("extensions/vscode-alysis" if managed_workflow else "candidate")
    candidate_dir.mkdir(parents=True)
    report = _completed_manual_provider_report()
    report.update(
        extension_version="0.3.0" if channel == "beta" else "0.4.0",
        platform_target=target,
        host_platform="linux",
    )
    candidate, trusted_key = _signed_candidate_vsix(
        candidate_dir, report, pre_release=channel == "beta"
    )
    pinned_key = tmp_path / "extensions/vscode-alysis/resources/managed-cli-release-public.pem"
    pinned_key.parent.mkdir(parents=True, exist_ok=True)
    pinned_key.write_bytes(trusted_key.read_bytes())
    monkeypatch.setattr(manual_validator, "PROJECT_ROOT", tmp_path)
    summary = _release_valid_dogfood(report, candidate)
    summary["host_platform"] = "linux"
    if managed_workflow:
        for label, compatibility in (("minimum", "minimum"), ("current-stable", "current_stable")):
            payload = dict(summary, vscode=summary["vscode_compatibility"][compatibility])
            (candidate_dir / f"production-dogfood-{label}-{target}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
    else:
        (candidate_dir / f"production-dogfood-{target}.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET", target)
    monkeypatch.setenv("RELEASE_CHANNEL", channel)
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))

    exec(compile(embedded[0], workflow_name, "exec"), {})

    if managed_workflow:
        combined = json.loads(
            (candidate_dir / f"production-dogfood-{target}.json").read_text(encoding="utf-8")
        )
        assert combined["extension_version"] == report["extension_version"]
        assert combined["vsix_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()


def _candidate_sbom(report: dict[str, Any], candidate: Path) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "vscode-alysis",
                "hashes": [
                    {
                        "alg": "SHA-256",
                        "content": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    }
                ],
                "properties": [
                    {
                        "name": "alysis:platform-target",
                        "value": report["platform_target"],
                    }
                ],
            }
        },
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("vsix", "other-candidate.vsix", "different VSIX"),
        ("vsix_sha256", "a" * 64, "VSIX SHA-256"),
        ("managed_cli_sha256", "b" * 64, "runtime hashes"),
        ("extension_version", "9.9.9", "extension version"),
        ("managed_artifact_version", "cli-9.9.9", "artifact version"),
        ("release_tag", "v9.9.9", "release identity"),
        ("source_sha", "2" * 40, "release identity"),
        ("source_repository", "https://example.test/fork", "release identity"),
    ],
)
def test_manual_provider_binding_rejects_mismatched_candidate_evidence(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    report[field] = value

    with pytest.raises(manual_validator.ManualReportValidationError, match=message):
        manual_validator.validate_candidate_binding(
            report,
            candidate,
            trusted_public_key_path=trusted_key,
        )


def test_manual_provider_binding_rejects_prerelease_marker(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report, pre_release=True)

    with pytest.raises(manual_validator.ManualReportValidationError, match="pre-release marker"):
        manual_validator.validate_candidate_binding(
            report,
            candidate,
            trusted_public_key_path=trusted_key,
        )


def test_manual_provider_binding_rejects_candidate_changed_after_report(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    candidate.write_bytes(candidate.read_bytes() + b"post-report mutation")

    with pytest.raises(manual_validator.ManualReportValidationError, match="VSIX SHA-256"):
        manual_validator.validate_candidate_binding(
            report,
            candidate,
            trusted_public_key_path=trusted_key,
        )


def test_manual_provider_binding_rejects_substituted_release_key(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, _trusted_key = _signed_candidate_vsix(tmp_path, report)
    unrelated_key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    pinned_key = tmp_path / "different-pinned-key.pem"
    pinned_key.write_bytes(unrelated_key)

    with pytest.raises(manual_validator.ManualReportValidationError, match="pinned release key"):
        manual_validator.validate_candidate_binding(
            report,
            candidate,
            trusted_public_key_path=pinned_key,
        )


def test_manual_provider_binding_rejects_tampered_manifest_signature(tmp_path: Path) -> None:
    report = _completed_manual_provider_report()
    candidate, trusted_key = _signed_candidate_vsix(tmp_path, report)
    with ZipFile(candidate) as archive:
        entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    manifest_name = "extension/resources/managed-cli/manifest.json"
    managed_manifest = json.loads(entries[manifest_name])
    managed_manifest["artifacts"][0]["signature"] = base64.b64encode(b"invalid").decode("ascii")
    entries[manifest_name] = json.dumps(managed_manifest, sort_keys=True).encode("utf-8")
    with ZipFile(candidate, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    report["vsix_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()

    with pytest.raises(manual_validator.ManualReportValidationError, match="signature is invalid"):
        manual_validator.validate_candidate_binding(
            report,
            candidate,
            trusted_public_key_path=trusted_key,
        )
