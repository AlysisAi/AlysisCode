from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vscode-remote-acceptance.yml"
DRIVER = (
    ROOT
    / "extensions"
    / "vscode-alysis"
    / "test"
    / "integration"
    / "remoteAcceptance"
    / "driver"
    / "extension.ts"
)


def test_real_remote_workflow_is_valid_yaml_and_uses_dedicated_protected_runners() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = yaml.safe_load(text)["jobs"]["remote-acceptance"]["steps"]
    python_ready = False
    for step in steps:
        if step.get("uses", "").startswith("actions/setup-python@"):
            assert step["with"]["python-version"] == "3.12"
            python_ready = True
        if "python " in step.get("run", ""):
            assert python_ready, f"Python is needed before {step['name']} on a fresh runner"
    for label in (
        "      - self-hosted",
        "      - windows",
        "      - x64",
        "      - alysis-release",
        "      - ${{ matrix.runner_label }}",
    ):
        assert label in text
    assert "runner_label: real-wsl" in text
    assert "runner_label: real-remote-ssh" in text
    assert "protected_environment: vscode-real-wsl-acceptance" in text
    assert "protected_environment: vscode-real-remote-ssh-acceptance" in text
    assert "environment: ${{ matrix.protected_environment }}" in text
    assert "needs: validate-release-source" in text
    assert "runs-on: ubuntu-latest" in text
    assert "validate_github_environment_policy.py" in text
    assert 'GH_API_VERSION: "2026-03-10"' in text


def test_real_remote_workflow_pins_and_attests_every_prerequisite_and_report() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    remote_job = text.split("  remote-acceptance:", 1)[1].split("  attest-remote-evidence:", 1)[0]
    hosted_attestor = text.split("  attest-remote-evidence:", 1)[1]
    assert "EXPECTED_VSCODE_VERSION: 1.90.0" in text
    assert "remote_extension_version: 0.88.2" in text
    assert "remote_extension_version: 0.112.0" in text
    assert "--vscode-sha256 $env:VSCODE_EXECUTABLE_SHA256" in text
    assert "--remote-extension-sha256 $env:REMOTE_EXTENSION_SHA256" in text
    assert "--interactive-session $env:SESSIONNAME" in text
    assert "name: vscode-alysis-linux-x64" in text
    assert (
        "Verify candidate provenance and exact SBOM before any install or execution" in remote_job
    )
    assert "validate_github_attestation_receipt.py" in remote_job
    assert "--expected-predicate $sbom" in remote_job
    assert remote_job.index("Verify candidate provenance") < remote_job.index(
        "Compile target and isolated remote acceptance driver"
    )
    assert "attestations: write" not in remote_job
    assert "id-token: write" not in remote_job
    assert "artifact-metadata: write" not in remote_job
    assert "attestations: read" in remote_job
    assert "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26" in hosted_attestor
    assert "--deny-self-hosted-runners" in hosted_attestor
    assert "select-latest" in hosted_attestor
    assert "wsl-${{ steps.select.outputs.wsl_attempt }}" in hosted_attestor
    assert "remote-ssh-${{ steps.select.outputs.remote_ssh_attempt }}" in hosted_attestor
    assert "retention-days: 90" in text
    assert "--candidate-run-id $env:CANDIDATE_RUN_ID" in text
    assert "--source-sha $env:GITHUB_SHA" in text


def test_remote_driver_asserts_real_host_runtime_oauth_and_reload_contracts() -> None:
    text = DRIVER.read_text(encoding="utf-8")
    for required in (
        "vscode.env.remoteName",
        'workspace.uri.scheme, "vscode-remote"',
        'process.platform, "linux"',
        'runtime.target, "linux-x64"',
        'runtime.origin, "managed"',
        'runtime.health?.status, "passed"',
        "mcpOAuthRemoteUnavailableReason",
        "workbench.action.reloadWindow",
        "runtime_identity_stable: true",
        "expected_hostname_sha256",
        "expected_workspace_path_sha256",
    ):
        assert required in text


def test_remote_workflow_never_uses_hosted_runner_for_remote_execution() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    remote_job = text.split("  remote-acceptance:", 1)[1].split("  attest-remote-evidence:", 1)[0]
    assert "ubuntu-latest" not in remote_job
    assert "runs-on: ubuntu-latest" in text.split("  attest-remote-evidence:", 1)[1]
    assert "windows-latest" not in text
    assert "macos-latest" not in text
