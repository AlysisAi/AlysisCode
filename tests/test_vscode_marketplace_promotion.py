from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.qa import validate_vscode_promotion_bundle as promotion_bundle
from scripts.qa.validate_vscode_promotion_bundle import (
    PromotionBundleValidationError,
    build_validation_receipt,
    verify_validation_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vscode-extension-promote.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_marketplace_promotion_requires_exact_official_tag_and_candidate_run() -> None:
    workflow = _workflow()
    validation = workflow.split("  validate-promotion:", 1)[1].split("  publish-marketplace:", 1)[0]

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "EXPECTED_REPOSITORY: AlysisAi/AlysisCode" in validation
    assert 'test "$REPOSITORY_PRIVATE" = "false"' in validation
    assert 'test "$REPOSITORY_VISIBILITY" = "public"' in validation
    assert 'test "$PUBLISH_CONFIRMATION" = "publish-$RELEASE_CHANNEL"' in validation
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in validation
    assert 'test "$(git rev-list -n 1 "$RELEASE_TAG")" = "$GITHUB_SHA"' in validation
    assert ".github/workflows/managed-cli-vsix-release.yml" in validation
    assert '.conclusion == "success"' in validation
    assert ".head_sha == $source_sha" in validation
    assert ".head_branch == $release_tag" in validation
    assert ".run_attempt == 1" in validation
    assert "evidence_run_id:" in workflow
    assert "installed_live_provider_run_id:" in workflow
    assert "untrusted_workspace_run_id:" in workflow
    assert "remote_acceptance_run_id:" in workflow
    assert "workflow_run_attempts:" in workflow
    assert "WORKFLOW_RUN_ATTEMPTS_JSON: ${{ inputs.workflow_run_attempts }}" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).evidence" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).installed_live_provider" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).untrusted_workspace" in workflow
    assert ".github/workflows/vscode-extension-evidence.yml" in validation
    assert '[[ "$EVIDENCE_RUN_ID" =~ ^[1-9][0-9]*$ ]]' in validation
    assert '[[ "$EVIDENCE_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]' in validation
    assert '[[ "$INSTALLED_LIVE_PROVIDER_RUN_ID" =~ ^[1-9][0-9]*$ ]]' in validation
    assert ".github/workflows/vscode-installed-live-provider-qa.yml" in validation
    assert ".github/workflows/vscode-extension-untrusted-workspace.yml" in validation
    assert ".github/workflows/vscode-remote-acceptance.yml" in validation
    assert validation.count(".run_attempt == $run_attempt") == 3
    assert validation.count("validate_github_environment_policy.py") == 2
    assert "vscode-release-evidence" in validation
    assert "managed-cli-release-assets" in validation
    assert '"vscode-marketplace-$RELEASE_CHANNEL"' in validation
    assert "vscode-installed-live-provider-qa" in validation
    assert "vscode-real-wsl-acceptance" in validation
    assert "vscode-real-remote-ssh-acceptance" in validation
    assert "--required-pattern 'v*'" in validation
    assert 'GH_API_VERSION: "2026-03-10"' in validation


def test_marketplace_promotion_revalidates_every_gate_before_secret_bearing_publish() -> None:
    workflow = _workflow()
    validation = workflow.split("  validate-promotion:", 1)[1].split(
        "  publish-managed-runtime-assets:", 1
    )[0]
    publish = workflow.split("  publish-marketplace:", 1)[1]

    assert "environment:" not in validation
    assert "secrets.VSCE_PAT" not in validation
    assert (
        "environment: ${{ inputs.channel == 'beta' && 'vscode-marketplace-beta' || 'vscode-marketplace-stable' }}"
        in publish
    )
    assert workflow.count("scripts/qa/validate_vscode_promotion_bundle.py") == 4
    assert workflow.count("--candidate-run-attempt 1") == 5
    assert workflow.count("--installed-live-provider-run-id") == 4
    assert workflow.count("--installed-live-provider-run-attempt") == 4
    assert workflow.count("--untrusted-workspace-run-id") == 4
    assert workflow.count("--untrusted-workspace-run-attempt") == 4
    assert workflow.count("--remote-acceptance-run-id") == 4
    assert workflow.count("--remote-acceptance-wsl-run-attempt") == 4
    assert workflow.count("--remote-acceptance-remote-ssh-run-attempt") == 4
    assert workflow.count('--evidence-run-attempt "$EVIDENCE_RUN_ATTEMPT"') == 5
    assert workflow.count('--promotion-run-attempt "$GITHUB_RUN_ATTEMPT"') == 5
    assert workflow.count("gh attestation verify") == 7
    assert "https://slsa.dev/provenance/v1" in validation
    assert "https://cyclonedx.org/bom" in validation
    assert "--deny-self-hosted-runners" in validation
    assert "--deny-self-hosted-runners" in publish
    assert publish.index("Revalidate package, evidence, and attestations") < publish.index(
        "Publish only missing exact target packages"
    )
    assert "/actions/runs/${CANDIDATE_RUN_ID}/approvals" in publish
    assert "/actions/runs/${EVIDENCE_RUN_ID}/approvals" in publish
    assert "/actions/runs/${GITHUB_RUN_ID}/approvals" in publish
    assert "validate_vscode_release_approvals.py" in publish
    assert "--candidate-run-attempt 1" in publish
    assert publish.index("bind recorded approvers") < publish.index(
        "Publish only missing exact target packages"
    )
    assert 'test "${#packages[@]}" -eq 6' in publish
    assert 'test "${#installed_live_provider_files[@]}" -eq 6' in validation
    assert 'test "${#untrusted_workspace_files[@]}" -eq 1' in validation
    assert 'test "${#remote_acceptance_files[@]}" -eq 2' in validation
    assert "beta) flags+=(--pre-release) ;;" in publish
    assert "stable) ;;" in publish
    assert '--packagePath "candidates/$name" "${flags[@]}"' in publish
    assert '--packagePath "candidates/$name"' in publish
    assert "reconcile_vscode_marketplace.py plan" in publish
    assert "reconcile_vscode_marketplace.py verify" in publish
    assert "--require-complete" in publish
    assert "--skip-duplicate" not in workflow
    assert "--allow-missing-repository" not in workflow
    assert "--allow-package-all-secrets" not in workflow
    assert workflow.count("Download exact attested evidence again after approval") == 2
    assert workflow.count("run-id: ${{ inputs.evidence_run_id }}") == 3
    assert 'gh release download "$RELEASE_TAG"' not in validation
    assert (
        "--verify-receipt attested-evidence/receipt/evidence-validation-receipt.json" in validation
    )


def test_promotion_verifies_exact_candidate_and_runtime_sbom_predicates() -> None:
    workflow = _workflow()

    assert workflow.count("validate_github_attestation_receipt.py") == 6
    assert workflow.count("--expected-predicate") == 3
    assert workflow.count('sbom="${package%.vsix}.cdx.json"') == 2
    assert 'sbom="managed-runtime/${target}.cdx.json"' in workflow


def test_marketplace_promotion_actions_are_immutable_and_receipt_is_retained() -> None:
    workflow = _workflow()
    validation = workflow.split("  validate-promotion:", 1)[1].split(
        "  publish-managed-runtime-assets:", 1
    )[0]
    runtime_publication = workflow.split("  publish-managed-runtime-assets:", 1)[1].split(
        "  publish-marketplace:", 1
    )[0]
    marketplace = workflow.split("  publish-marketplace:", 1)[1]

    uses = re.findall(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$", workflow, re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref, _comment in uses)
    assert all(comment for _ref, comment in uses)
    assert "vscode-marketplace-publication.json" in workflow
    assert "validate_vscode_production_signoff" in Path(
        "scripts/qa/validate_vscode_promotion_bundle.py"
    ).read_text(encoding="utf-8")
    assert "build_vscode_marketplace_verification.py" in marketplace
    assert "vscode-marketplace-verification/**/*" in marketplace
    assert "marketplace-published" in marketplace
    assert "public-packages" in marketplace
    assert "attestations: write" in marketplace
    assert "artifact-metadata: write" in marketplace
    assert "id-token: write" in marketplace
    assert '--promotion-run-attempt "$GITHUB_RUN_ATTEMPT"' in marketplace
    assert "${{ github.run_id }}-attempt-${{ github.run_attempt }}" in marketplace
    assert "retention-days: 90" in workflow
    assert "contents: write" not in validation
    assert "contents: write" in runtime_publication
    assert "environment: managed-cli-release-assets" in runtime_publication
    assert "--clobber" not in runtime_publication
    assert "contents: write" not in marketplace


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_promotion_receipt_freezes_all_candidate_evidence_across_approval(
    tmp_path: Path,
    channel: str,
) -> None:
    candidates = tmp_path / "candidates"
    evidence = tmp_path / "evidence"
    runtime = tmp_path / "runtime"
    installed_live_provider = tmp_path / "installed-live-provider"
    untrusted_workspace = tmp_path / "untrusted-workspace"
    remote_acceptance = tmp_path / "remote-acceptance"
    for directory, filename in (
        (candidates, "candidate.vsix"),
        (evidence, "report.json"),
        (runtime, "manifest.json"),
        (installed_live_provider, "live-provider.json"),
        (untrusted_workspace, "untrusted.json"),
        (remote_acceptance, "remote.json"),
    ):
        directory.mkdir()
        (directory / filename).write_text(filename, encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt = build_validation_receipt(
        candidates,
        evidence,
        runtime,
        installed_live_provider,
        untrusted_workspace,
        remote_acceptance,
        channel=channel,
        release_tag="v1.2.3",
        source_sha="a" * 40,
        candidate_run_id=123,
        candidate_run_attempt=1,
        installed_live_provider_run_id=456,
        installed_live_provider_run_attempt=2,
        untrusted_workspace_run_id=789,
        untrusted_workspace_run_attempt=3,
        remote_acceptance_run_id=1011,
        remote_acceptance_wsl_run_attempt=2,
        remote_acceptance_remote_ssh_run_attempt=3,
        evidence_run_id=1213,
        evidence_run_attempt=4,
        promotion_run_id=1415,
        promotion_run_attempt=5,
    )
    assert receipt["schema_version"] == 7
    assert receipt["channel"] == channel
    assert receipt["candidate_run_attempt"] == 1
    assert receipt["installed_live_provider_run_attempt"] == 2
    assert receipt["untrusted_workspace_run_attempt"] == 3
    assert receipt["evidence_run_attempt"] == 4
    assert receipt["promotion_run_attempt"] == 5
    assert receipt["remote_acceptance_wsl_run_attempt"] == 2
    assert receipt["remote_acceptance_remote_ssh_run_attempt"] == 3
    receipt_path.write_text(__import__("json").dumps(receipt), encoding="utf-8")

    verify_validation_receipt(
        receipt_path,
        candidates,
        evidence,
        runtime,
        installed_live_provider,
        untrusted_workspace,
        remote_acceptance,
        channel=channel,
        release_tag="v1.2.3",
        source_sha="a" * 40,
        candidate_run_id=123,
        candidate_run_attempt=1,
        installed_live_provider_run_id=456,
        installed_live_provider_run_attempt=2,
        untrusted_workspace_run_id=789,
        untrusted_workspace_run_attempt=3,
        remote_acceptance_run_id=1011,
        remote_acceptance_wsl_run_attempt=2,
        remote_acceptance_remote_ssh_run_attempt=3,
        evidence_run_id=1213,
        evidence_run_attempt=4,
        promotion_run_id=1415,
        promotion_run_attempt=5,
    )

    with pytest.raises(PromotionBundleValidationError, match="changed after protected validation"):
        verify_validation_receipt(
            receipt_path,
            candidates,
            evidence,
            runtime,
            installed_live_provider,
            untrusted_workspace,
            remote_acceptance,
            channel=channel,
            release_tag="v1.2.3",
            source_sha="a" * 40,
            candidate_run_id=123,
            candidate_run_attempt=1,
            installed_live_provider_run_id=456,
            installed_live_provider_run_attempt=2,
            untrusted_workspace_run_id=789,
            untrusted_workspace_run_attempt=3,
            remote_acceptance_run_id=1011,
            remote_acceptance_wsl_run_attempt=2,
            remote_acceptance_remote_ssh_run_attempt=3,
            evidence_run_id=1213,
            evidence_run_attempt=6,
            promotion_run_id=1415,
            promotion_run_attempt=5,
        )

    (installed_live_provider / "live-provider.json").write_text("changed", encoding="utf-8")
    with pytest.raises(PromotionBundleValidationError, match="changed after protected validation"):
        verify_validation_receipt(
            receipt_path,
            candidates,
            evidence,
            runtime,
            installed_live_provider,
            untrusted_workspace,
            remote_acceptance,
            channel=channel,
            release_tag="v1.2.3",
            source_sha="a" * 40,
            candidate_run_id=123,
            candidate_run_attempt=1,
            installed_live_provider_run_id=456,
            installed_live_provider_run_attempt=2,
            untrusted_workspace_run_id=789,
            untrusted_workspace_run_attempt=3,
            remote_acceptance_run_id=1011,
            remote_acceptance_wsl_run_attempt=2,
            remote_acceptance_remote_ssh_run_attempt=3,
            evidence_run_id=1213,
            evidence_run_attempt=4,
            promotion_run_id=1415,
            promotion_run_attempt=5,
        )


def test_promotion_validator_binds_each_remote_environment_to_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = tmp_path / "candidates"
    untrusted = tmp_path / "untrusted"
    remote = tmp_path / "remote"
    candidates.mkdir()
    untrusted.mkdir()
    remote.mkdir()
    (untrusted / "vscode-alysis-linux-x64.untrusted-workspace.json").write_text(
        __import__("json").dumps(
            {
                "release_tag": "v1.2.3",
                "source_sha": "a" * 40,
                "candidate_run_id": 123,
                "workflow_run_id": 456,
                "workflow_run_attempt": 2,
                "platform_target": "linux-x64",
            }
        ),
        encoding="utf-8",
    )
    for environment in ("wsl", "remote_ssh"):
        (remote / f"vscode-remote-acceptance-{environment}.json").write_text(
            __import__("json").dumps(
                {
                    "vscode": {"commit": "b" * 40},
                    "host": {
                        "authority_sha256": "c" * 64,
                        "hostname_sha256": "d" * 64,
                        "workspace_path_sha256": "e" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )

    observed: dict[str, int] = {}

    monkeypatch.setattr(
        promotion_bundle, "validate_untrusted_workspace_candidate", lambda *_a, **_k: None
    )

    def capture_remote(_report: object, _candidate: Path, **expected: object) -> None:
        observed[str(expected["expected_environment"])] = int(
            expected["expected_workflow_run_attempt"]  # type: ignore[arg-type]
        )

    monkeypatch.setattr(promotion_bundle, "validate_remote_acceptance_evidence", capture_remote)
    promotion_bundle._validate_installed_workspace_acceptance(
        candidates,
        untrusted,
        remote,
        release_tag="v1.2.3",
        source_sha="a" * 40,
        candidate_run_id=123,
        untrusted_workspace_run_id=456,
        untrusted_workspace_run_attempt=2,
        remote_acceptance_run_id=789,
        remote_acceptance_wsl_run_attempt=2,
        remote_acceptance_remote_ssh_run_attempt=3,
        trusted_public_key_path=None,
    )

    assert observed == {"wsl": 2, "remote_ssh": 3}

    with pytest.raises(
        PromotionBundleValidationError,
        match="Untrusted-workspace evidence does not bind",
    ):
        promotion_bundle._validate_installed_workspace_acceptance(
            candidates,
            untrusted,
            remote,
            release_tag="v1.2.3",
            source_sha="a" * 40,
            candidate_run_id=123,
            untrusted_workspace_run_id=456,
            untrusted_workspace_run_attempt=3,
            remote_acceptance_run_id=789,
            remote_acceptance_wsl_run_attempt=2,
            remote_acceptance_remote_ssh_run_attempt=3,
            trusted_public_key_path=None,
        )
