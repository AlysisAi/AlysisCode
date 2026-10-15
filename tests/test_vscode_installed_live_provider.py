from __future__ import annotations

import hashlib
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.qa import validate_vscode_installed_live_provider as validator

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / ".github" / "release-policy" / "vscode-live-provider-origins.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_dogfood(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "extension_version": "1.2.3",
                "vscode_version": "1.90.0",
                "host_platform": "linux",
                "host_arch": "x64",
                "extension_mode": "production",
                "runtime_origin": "managed",
                "runtime_production": True,
                "managed_artifact_version": "1.2.3-linux-x64",
                "managed_cli_version": "1.2.3",
                "managed_protocol_version": "1",
                "managed_cli_sha256": "b" * 64,
                "release_tag": "v1.2.3",
                "source_repository": "https://github.com/AlysisAi/AlysisCode",
                "source_sha": "a" * 40,
                "platform_target": "linux-x64",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _report(candidate: Path, dogfood: Path) -> dict[str, object]:
    started = datetime.now(UTC).replace(microsecond=0)
    return {
        "schema_name": validator.SCHEMA_NAME,
        "schema_version": validator.SCHEMA_VERSION,
        "status": "passed",
        "mode": validator.SCHEMA_NAME,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "completed_at": (started + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "release_tag": "v1.2.3",
        "source_sha": "a" * 40,
        "candidate_run_id": "12345",
        "workflow_run_id": 45678,
        "workflow_run_attempt": 2,
        "production_dogfood_sha256": _sha256(dogfood),
        "platform_target": "linux-x64",
        "vsix_sha256": _sha256(candidate),
        "extension_version": "1.2.3",
        "vscode_version": "1.90.0",
        "host_platform": "linux",
        "host_arch": "x64",
        "extension_mode": "production",
        "runtime_origin": "managed",
        "runtime_production": True,
        "managed_artifact_version": "1.2.3-linux-x64",
        "managed_cli_version": "1.2.3",
        "managed_protocol_version": "1",
        "managed_cli_sha256": "b" * 64,
        "source_repository": "https://github.com/AlysisAi/AlysisCode",
        "provider": "openai-responses",
        "model": "provider-model",
        "provider_origin": "https://api.openai.com",
        "provider_origin_policy_sha256": _sha256(POLICY),
        "readonly_mode": True,
        "response": {
            "length": 42,
            "sha256": "c" * 64,
            "marker_matched": True,
            "elapsed_ms": 1234,
        },
        "receipts": {name: "passed" for name in validator.RECEIPTS},
        "extension_host_launches": 2,
        "retained_response_content": False,
    }


def _validate(
    report: dict[str, object], candidate: Path, dogfood: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        validator,
        "validate_production_dogfood_candidate_binding",
        lambda _payload, _candidate, **_kwargs: None,
    )
    validator.validate_installed_live_provider_evidence(
        report,
        candidate_vsix=candidate,
        production_dogfood=dogfood,
        expected_target="linux-x64",
        expected_release_tag="v1.2.3",
        expected_source_sha="a" * 40,
        expected_candidate_run_id="12345",
        expected_workflow_run_id=45678,
        expected_workflow_run_attempt=2,
        provider_origin_policy=POLICY,
    )


def test_installed_live_provider_evidence_accepts_redacted_bound_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)

    _validate(_report(candidate, dogfood), candidate, dogfood, monkeypatch)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("runtime_origin",), "development", "runtime must be managed production"),
        (("readonly_mode",), False, "not readonly"),
        (("response", "marker_matched"), False, "marker did not match"),
        (("retained_response_content",), True, "retention must be disabled"),
        (("extension_host_launches",), 1, "two Extension Host launches"),
    ],
)
def test_installed_live_provider_evidence_rejects_false_safety_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    target: dict[str, object] = report
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[assignment]
    target[path[-1]] = value

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match=message):
        _validate(report, candidate, dogfood, monkeypatch)


def test_installed_live_provider_evidence_rejects_candidate_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    candidate.write_bytes(b"substituted-vsix")

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="exact candidate"):
        _validate(report, candidate, dogfood, monkeypatch)


def test_installed_live_provider_evidence_rejects_retained_response_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    response = report["response"]
    assert isinstance(response, dict)
    response["text"] = "raw provider response must never be retained"

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="fields are not exact"):
        _validate(report, candidate, dogfood, monkeypatch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extension_version", "9.9.9"),
        ("vscode_version", "1.91.0"),
        ("managed_artifact_version", "1.2.3-other"),
        ("managed_cli_version", "1.2.4"),
        ("managed_protocol_version", "2"),
        ("managed_cli_sha256", "c" * 64),
    ],
)
def test_installed_live_provider_evidence_rejects_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report[field] = value

    with pytest.raises(
        validator.InstalledLiveProviderEvidenceError,
        match=rf"{field} does not match the exact production dogfood runtime",
    ):
        _validate(report, candidate, dogfood, monkeypatch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "p" * 129),
        ("provider", "provider\nunsafe"),
        ("provider", "api_key=retained-value"),
        ("model", "sk-abcdefghijklmnopqrstuv"),
        ("model", "m" * 257),
    ],
)
def test_installed_live_provider_evidence_rejects_unsafe_provider_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report[field] = value

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="safe retained"):
        _validate(report, candidate, dogfood, monkeypatch)


@pytest.mark.parametrize(
    "origin",
    [
        "http://api.provider.example",
        "https://user@api.provider.example",
        "https://api.provider.example/v1",
        "https://api.provider.example:8443",
        "https://localhost",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://10.0.0.1",
        "https://API.provider.example",
    ],
)
def test_installed_live_provider_evidence_rejects_unsafe_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report["provider_origin"] = origin

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="provider_origin"):
        _validate(report, candidate, dogfood, monkeypatch)


def test_installed_live_provider_evidence_bounds_response_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    response = report["response"]
    assert isinstance(response, dict)
    response["length"] = 97

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="between 1 and 96"):
        _validate(report, candidate, dogfood, monkeypatch)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("provider_origin", "https://attacker.example", "not authorized"),
        ("provider", "custom", "not authorized"),
        ("provider_origin_policy_sha256", "d" * 64, "does not bind"),
    ),
)
def test_installed_live_provider_evidence_binds_committed_destination_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report[field] = value

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match=message):
        _validate(report, candidate, dogfood, monkeypatch)


def test_installed_live_provider_workflow_is_manual_protected_and_six_target() -> None:
    workflow = Path(".github/workflows/vscode-installed-live-provider-qa.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "schedule:" not in workflow
    assert "environment: vscode-installed-live-provider-qa" in workflow
    assert "timeout-minutes: 90" in workflow
    assert "needs: validate-release-source" in workflow
    assert "validate_github_environment_policy.py" in workflow
    assert 'GH_API_VERSION: "2026-03-10"' in workflow
    assert "actions: read" in workflow
    for target in validator.TARGET_HOSTS:
        assert f"target: {target}" in workflow
    assert "candidate_run_id:" in workflow
    assert "ALYSIS_WORKFLOW_RUN_ID: ${{ github.run_id }}" in workflow
    assert "ALYSIS_WORKFLOW_RUN_ATTEMPT: ${{ github.run_attempt }}" in workflow
    assert '--workflow-run-id "$GITHUB_RUN_ID"' in workflow
    assert '--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"' in workflow
    assert "-attempt-${{ github.run_attempt }}" in workflow
    assert 'test "$GITHUB_REF" = "refs/tags/$REQUESTED_TAG"' in workflow
    assert '"path": ".github/workflows/managed-cli-vsix-release.yml"' in workflow
    assert "gh attestation verify" in workflow
    assert "test:production-install" not in workflow
    assert "production-live-provider/runTest.js" in workflow
    assert "actions/attest@" in workflow
    assert "installed-live-provider.json" in workflow
    assert workflow.count("persist-credentials: false") >= 2
    assert workflow.count("vars.ALYSIS_LIVE_QA_APPROVED_ORIGIN") == 3
    assert "validate_vscode_live_provider_policy.py" in workflow
    assert "ALYSIS_LIVE_QA_ORIGIN_POLICY_SHA256" in workflow
    assert "--provider-origin-policy" in workflow
    assert "require_vscode_compatibility=True" in workflow


def test_installed_live_provider_candidate_identity_python_is_syntactically_valid() -> None:
    workflow = Path(".github/workflows/vscode-installed-live-provider-qa.yml").read_text(
        encoding="utf-8"
    )
    embedded = workflow.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]

    compile(textwrap.dedent(embedded), "candidate-identity-inline.py", "exec")


def test_installed_live_provider_evidence_rejects_zero_candidate_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report["candidate_run_id"] = "0"

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match="run id is invalid"):
        validator.validate_installed_live_provider_evidence(
            report,
            candidate_vsix=candidate,
            production_dogfood=dogfood,
            expected_target="linux-x64",
            expected_release_tag="v1.2.3",
            expected_source_sha="a" * 40,
            expected_candidate_run_id="0",
            expected_workflow_run_id=45678,
            expected_workflow_run_attempt=2,
            provider_origin_policy=POLICY,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workflow_run_id", 45679, "workflow_run_id does not match"),
        ("workflow_run_attempt", 3, "workflow_run_attempt does not match"),
    ],
)
def test_installed_live_provider_evidence_rejects_attempt_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
    message: str,
) -> None:
    candidate = tmp_path / "vscode-alysis-linux-x64.vsix"
    dogfood = tmp_path / "production-dogfood-linux-x64.json"
    candidate.write_bytes(b"candidate-vsix")
    _write_dogfood(dogfood)
    report = _report(candidate, dogfood)
    report[field] = value

    with pytest.raises(validator.InstalledLiveProviderEvidenceError, match=message):
        _validate(report, candidate, dogfood, monkeypatch)


def test_real_provider_secret_is_scoped_only_to_live_execution_steps() -> None:
    workflow = Path(".github/workflows/vscode-installed-live-provider-qa.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("secrets.ALYSIS_LIVE_API_KEY") == 2
    assert "ALYSIS_LIVE_API_KEY:" in workflow
    assert "env:\n  ALYSIS_LIVE_API_KEY" not in workflow
    assert "retained_response_content" not in workflow
