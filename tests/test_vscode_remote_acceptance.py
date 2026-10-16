from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.qa import run_vscode_remote_acceptance as remote_runner
from scripts.qa.run_vscode_remote_acceptance import stage_driver
from scripts.qa.validate_vscode_remote_acceptance import (
    RemoteAcceptanceError,
    candidate_binding,
    select_latest_evidence,
    validate_evidence,
    validate_prerequisites,
)

SOURCE_SHA = "a" * 40
VSIX_SHA = "b" * 64
MANAGED_SHA = "c" * 64
VSCODE_COMMIT = "d" * 40
AUTHORITY_SHA = "e" * 64
HOSTNAME_SHA = "f" * 64
WORKSPACE_SHA = "1" * 64
VSCODE_EXE_SHA = "6" * 64
PREREQUISITE_SHA = "7" * 64


def _write_vsix(
    path: Path,
    *,
    publisher: str,
    name: str,
    version: str,
    target: str | None = None,
) -> str:
    target_attribute = f' TargetPlatform="{target}"' if target else ""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "extension/package.json",
            json.dumps({"publisher": publisher, "name": name, "version": version}),
        )
        archive.writestr(
            "extension.vsixmanifest",
            f"<PackageManifest><Metadata><Identity{target_attribute}/></Metadata></PackageManifest>",
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_dir(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    vsix = candidate / "vscode-alysis-linux-x64.vsix"
    digest = _write_vsix(
        vsix,
        publisher="alysisai",
        name="vscode-alysis",
        version="1.2.3",
        target="linux-x64",
    )
    (candidate / "production-dogfood-linux-x64.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "mode": "installed-production-vsix",
                "release_valid": True,
                "platform_target": "linux-x64",
                "vsix_sha256": digest,
                "managed_cli_sha256": MANAGED_SHA,
                "managed_artifact_version": "1.2.3+release.1",
                "vscode_compatibility": {
                    "minimum": {
                        "requested_version": "1.90.0",
                        "version": "1.90.0",
                        "commit": "3" * 40,
                        "arch": "x64",
                        "executable_sha256": "4" * 64,
                    },
                    "current_stable": {
                        "requested_version": "stable",
                        "version": "1.105.0",
                        "commit": "5" * 40,
                        "arch": "x64",
                        "executable_sha256": "6" * 64,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return candidate


def _report(candidate: Path, environment: str = "wsl") -> dict[str, object]:
    binding = candidate_binding(candidate)
    remote = {
        "wsl": {
            "id": "ms-vscode-remote.remote-wsl",
            "version": "0.88.2",
            "remote_name": "wsl",
            "runner_role": "real-wsl",
        },
        "remote_ssh": {
            "id": "ms-vscode-remote.remote-ssh",
            "version": "0.112.0",
            "remote_name": "ssh-remote",
            "runner_role": "real-remote-ssh",
        },
    }[environment]
    return {
        "schema_name": "vscode-remote-installed-vsix-acceptance",
        "schema_version": 2,
        "status": "passed",
        "environment": environment,
        "release_tag": "v1.2.3",
        "source_sha": SOURCE_SHA,
        "candidate_run_id": 101,
        "workflow_run_id": 202,
        "workflow_run_attempt": 1,
        "acceptance_id": f"202-1-{environment}",
        "started_at": "2026-07-30T12:00:00Z",
        "completed_at": "2026-07-30T12:02:00Z",
        "extension": {
            "id": "alysisai.vscode-alysis",
            "version": binding["extension_version"],
            "mode": "production",
            "vsix_name": binding["vsix_name"],
            "vsix_sha256": binding["vsix_sha256"],
            "installed_path_sha256": "2" * 64,
        },
        "vscode": {
            "version": "1.90.0",
            "commit": VSCODE_COMMIT,
            "executable_sha256": VSCODE_EXE_SHA,
        },
        "prerequisite": {
            "id": remote["id"],
            "version": remote["version"],
            "sha256": PREREQUISITE_SHA,
        },
        "host": {
            "platform": "linux",
            "arch": "x64",
            "remote_name": remote["remote_name"],
            "runner_role": remote["runner_role"],
            "authority_sha256": AUTHORITY_SHA,
            "hostname_sha256": HOSTNAME_SHA,
            "workspace_scheme": "vscode-remote",
            "workspace_path_sha256": WORKSPACE_SHA,
            "workspace_trusted": True,
            "workspace_trust_enabled": True,
            "workspace_trust_grant_method": "workspace_trust_editor_ctrl_enter",
            "workspace_trust_initial_state": "restricted",
            "workspace_trust_parent_grant": False,
        },
        "runtime": {
            "origin": "managed",
            "production": True,
            "target": "linux-x64",
            "artifact_version": binding["managed_artifact_version"],
            "cli_version": "1.2.3",
            "protocol_version": "1",
            "sha256": binding["managed_cli_sha256"],
            "release_tag": "v1.2.3",
            "source_repository": "https://github.com/AlysisAi/AlysisCode",
            "source_sha": SOURCE_SHA,
            "signature_verified": True,
        },
        "reload": {
            "phases": 2,
            "profile_state_reused": True,
            "runtime_identity_stable": True,
        },
        "checks": {
            "bridge_health": "passed",
            "clean_install": "passed",
            "extension_reload_recovery": "passed",
            "managed_runtime": "passed",
            "oauth_fail_closed": "passed",
            "remote_identity": "passed",
            "secret_leak_check": "passed",
            "workspace_trust_enforced": "passed",
        },
    }


def _validate(report: dict[str, object], candidate: Path, environment: str = "wsl") -> None:
    validate_evidence(
        report,
        candidate,
        expected_environment=environment,
        expected_release_tag="v1.2.3",
        expected_source_sha=SOURCE_SHA,
        expected_candidate_run_id=101,
        expected_workflow_run_id=202,
        expected_workflow_run_attempt=1,
        expected_vscode_version="1.90.0",
        expected_vscode_commit=VSCODE_COMMIT,
        expected_vscode_executable_sha256=VSCODE_EXE_SHA,
        expected_prerequisite_sha256=PREREQUISITE_SHA,
        expected_authority_sha256=AUTHORITY_SHA,
        expected_hostname_sha256=HOSTNAME_SHA,
        expected_workspace_path_sha256=WORKSPACE_SHA,
    )


@pytest.mark.parametrize("environment", ["wsl", "remote_ssh"])
def test_remote_evidence_accepts_only_complete_release_bound_reports(
    tmp_path: Path, environment: str
) -> None:
    candidate = _candidate_dir(tmp_path)
    _validate(_report(candidate, environment), candidate, environment)


def test_remote_evidence_rejects_a_local_extension_host(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    report = _report(candidate)
    report["host"]["remote_name"] = ""  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError, match="real Linux remote host"):
        _validate(report, candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_trust_enabled", False),
        ("workspace_trust_grant_method", "database_seed"),
        ("workspace_trust_initial_state", "trusted"),
        ("workspace_trust_parent_grant", True),
    ),
)
def test_remote_evidence_rejects_disabled_or_unbound_workspace_trust(
    tmp_path: Path, field: str, value: object
) -> None:
    candidate = _candidate_dir(tmp_path)
    report = _report(candidate)
    report["host"][field] = value  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError):
        _validate(report, candidate)


def test_remote_evidence_rejects_runtime_substitution_and_incomplete_reload(
    tmp_path: Path,
) -> None:
    candidate = _candidate_dir(tmp_path)
    substituted = _report(candidate)
    substituted["runtime"]["sha256"] = "9" * 64  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError, match="not release-bound"):
        _validate(substituted, candidate)
    no_reload = _report(candidate)
    no_reload["reload"]["phases"] = 1  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError, match="reload/recovery"):
        _validate(no_reload, candidate)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("vscode", "executable_sha256", "8" * 64),
        ("prerequisite", "id", "evil.remote-extension"),
        ("prerequisite", "version", "99.0.0"),
        ("prerequisite", "sha256", "8" * 64),
        ("host", "runner_role", "hosted"),
    ),
)
def test_remote_evidence_rejects_toolchain_or_runner_substitution(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    candidate = _candidate_dir(tmp_path)
    report = _report(candidate)
    report[section][field] = value  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError):
        _validate(report, candidate)


def test_remote_evidence_rejects_candidate_mutation(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    report = _report(candidate)
    with (candidate / "vscode-alysis-linux-x64.vsix").open("ab") as handle:
        handle.write(b"mutated")
    with pytest.raises(RemoteAcceptanceError, match="dogfood does not bind"):
        _validate(report, candidate)


def test_remote_acceptance_rejects_candidate_without_current_stable_compatibility(
    tmp_path: Path,
) -> None:
    candidate = _candidate_dir(tmp_path)
    dogfood_path = candidate / "production-dogfood-linux-x64.json"
    dogfood = json.loads(dogfood_path.read_text(encoding="utf-8"))
    dogfood.pop("vscode_compatibility")
    dogfood["schema_version"] = 2
    dogfood_path.write_text(json.dumps(dogfood), encoding="utf-8")
    with pytest.raises(RemoteAcceptanceError, match="dual-version production dogfood"):
        candidate_binding(candidate)


def test_remote_evidence_rejects_secret_looking_values(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    report = _report(candidate)
    report["runtime"]["protocol_version"] = "Bearer abcdef"  # type: ignore[index]
    with pytest.raises(RemoteAcceptanceError, match="secret-looking"):
        _validate(report, candidate)


def test_prerequisite_preflight_requires_real_role_hashes_and_exact_vsix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = tmp_path / "Code.exe"
    code.write_bytes(b"pinned-code")
    code_sha = hashlib.sha256(code.read_bytes()).hexdigest()
    prerequisite = tmp_path / "remote-wsl.vsix"
    prerequisite_sha = _write_vsix(
        prerequisite,
        publisher="ms-vscode-remote",
        name="remote-wsl",
        version="0.88.2",
    )
    monkeypatch.setattr(
        "scripts.qa.validate_vscode_remote_acceptance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=f"1.90.0\n{VSCODE_COMMIT}\nx64\n", stderr=""
        ),
    )
    identity = validate_prerequisites(
        environment="wsl",
        runner_role="real-wsl",
        vscode_executable=code,
        expected_vscode_sha256=code_sha,
        expected_vscode_version="1.90.0",
        remote_extension_vsix=prerequisite,
        expected_remote_extension_sha256=prerequisite_sha,
        expected_remote_extension_version="0.88.2",
        interactive_session="Console",
    )
    assert identity == {
        "vscode_commit": VSCODE_COMMIT,
        "remote_extension_id": "ms-vscode-remote.remote-wsl",
    }
    with pytest.raises(RemoteAcceptanceError, match="local/hosted substitution"):
        validate_prerequisites(
            environment="wsl",
            runner_role="hosted",
            vscode_executable=code,
            expected_vscode_sha256=code_sha,
            expected_vscode_version="1.90.0",
            remote_extension_vsix=prerequisite,
            expected_remote_extension_sha256=prerequisite_sha,
            expected_remote_extension_version="0.88.2",
            interactive_session="Console",
        )


def test_driver_staging_is_minimal_and_refuses_overwrite(tmp_path: Path) -> None:
    compiled = tmp_path / "extension.js"
    compiled.write_text("exports.activate = async () => {};\n", encoding="utf-8")
    staged = tmp_path / "driver"
    stage_driver(compiled, staged)
    package = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert package["publisher"] == "alysisai"
    assert package["name"] == "alysis-remote-acceptance-driver"
    assert package["extensionKind"] == ["workspace"]
    assert package["capabilities"] == {"untrustedWorkspaces": {"supported": True}}
    assert sorted(path.name for path in staged.iterdir()) == ["extension.js", "package.json"]
    with pytest.raises(RemoteAcceptanceError, match="already exists"):
        stage_driver(compiled, staged)


def test_workspace_trust_uses_the_pinned_ui_action_over_loopback_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                [
                    {
                        "type": "page",
                        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/one",
                    }
                ]
            ).encode()

    class Client:
        def __init__(self, endpoint: str, timeout: float) -> None:
            assert endpoint == "ws://127.0.0.1:9222/devtools/page/one"
            assert timeout > 0

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> None:
            calls.append((method, params))

    monkeypatch.setattr(remote_runner, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(remote_runner, "_CdpWebSocket", Client)
    remote_runner.grant_workspace_trust_via_cdp(9222, timeout=1)

    assert calls == [
        (
            "Input.dispatchKeyEvent",
            {
                "code": "Enter",
                "key": "Enter",
                "modifiers": 2,
                "nativeVirtualKeyCode": 13,
                "windowsVirtualKeyCode": 13,
                "type": "rawKeyDown",
            },
        ),
        (
            "Input.dispatchKeyEvent",
            {
                "code": "Enter",
                "key": "Enter",
                "modifiers": 2,
                "nativeVirtualKeyCode": 13,
                "windowsVirtualKeyCode": 13,
                "type": "keyUp",
            },
        ),
    ]


def test_remote_runner_never_disables_or_database_seeds_workspace_trust() -> None:
    source = Path(remote_runner.__file__).read_text(encoding="utf-8")
    assert '"security.workspace.trust.enabled": False' not in source
    assert "content.trust.model.key" not in source
    assert '"security.workspace.trust.enabled": True' in source
    assert 'f"--remote-debugging-port={devtools_port}"' in source
    assert remote_runner.PINNED_TRUST_UI_COMMITS == {
        "1.90.0": "89de5a8d4d6205e5b11647eb6a74844ca23d2573"
    }


def test_report_schema_rejects_unrecognized_fields(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    report = deepcopy(_report(candidate))
    report["raw_hostname"] = "sensitive-host"
    with pytest.raises(RemoteAcceptanceError, match="exact schema"):
        _validate(report, candidate)


def _write_raw_report(
    raw: Path,
    candidate: Path,
    *,
    environment: str,
    attempt: int,
    reported_attempt: int | None = None,
) -> None:
    artifact = raw / (
        f"vscode-remote-acceptance-raw-{environment}-{SOURCE_SHA}-101-attempt-{attempt}"
    )
    artifact.mkdir(parents=True)
    report = _report(candidate, environment)
    report_attempt = attempt if reported_attempt is None else reported_attempt
    report["workflow_run_attempt"] = report_attempt
    report["acceptance_id"] = f"202-{report_attempt}-{environment}"
    (artifact / f"vscode-remote-acceptance-{environment}.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def test_remote_selector_accepts_explicit_mixed_attempts_and_selects_latest(
    tmp_path: Path,
) -> None:
    candidate = _candidate_dir(tmp_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    _write_raw_report(raw, candidate, environment="wsl", attempt=1)
    _write_raw_report(raw, candidate, environment="wsl", attempt=3)
    _write_raw_report(raw, candidate, environment="remote_ssh", attempt=2)
    output = tmp_path / "selected"

    attempts = select_latest_evidence(
        raw,
        candidate,
        output,
        expected_release_tag="v1.2.3",
        expected_source_sha=SOURCE_SHA,
        expected_candidate_run_id=101,
        expected_workflow_run_id=202,
        maximum_workflow_run_attempt=3,
        expected_vscode_version="1.90.0",
    )

    assert attempts == {"remote_ssh": 2, "wsl": 3}
    assert sorted(path.name for path in output.iterdir()) == [
        "vscode-remote-acceptance-remote_ssh.json",
        "vscode-remote-acceptance-wsl.json",
    ]


def test_remote_selector_rejects_artifact_report_attempt_mismatch(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    _write_raw_report(raw, candidate, environment="wsl", attempt=1, reported_attempt=2)
    _write_raw_report(raw, candidate, environment="remote_ssh", attempt=1)

    with pytest.raises(RemoteAcceptanceError, match="workflow_run_attempt"):
        select_latest_evidence(
            raw,
            candidate,
            tmp_path / "selected",
            expected_release_tag="v1.2.3",
            expected_source_sha=SOURCE_SHA,
            expected_candidate_run_id=101,
            expected_workflow_run_id=202,
            maximum_workflow_run_attempt=2,
            expected_vscode_version="1.90.0",
        )
