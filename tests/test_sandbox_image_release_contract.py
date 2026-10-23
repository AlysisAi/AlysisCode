from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sandbox-image.yml"
DOCKERFILE = ROOT / "scripts" / "sandbox" / "Dockerfile"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_sandbox_release_uses_an_isolated_validated_tag_namespace() -> None:
    workflow = _workflow()

    assert '- "sandbox-v*"' in workflow
    assert re.search(r'^\s+- "v\*"$', workflow, flags=re.MULTILINE) is None
    assert r"^sandbox-v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$" in workflow
    assert 'test "${REPOSITORY_PRIVATE}" = "false"' in workflow
    assert 'test "${REPOSITORY_VISIBILITY}" = "public"' in workflow
    assert 'test "${GITHUB_REF}" = "refs/heads/${DEFAULT_BRANCH}"' in workflow
    assert 'test "$(git rev-parse "${REF_NAME}^{commit}")" = "${GITHUB_SHA}"' in workflow
    assert 'git merge-base --is-ancestor "${GITHUB_SHA}" "origin/${DEFAULT_BRANCH}"' in workflow


def test_consumer_aliases_are_only_written_after_every_candidate_gate() -> None:
    workflow = _workflow()
    candidate = workflow.split("  build-verify-candidate:", 1)[1].split("  promote:", 1)[0]
    promotion = workflow.split("  promote:", 1)[1]
    promotion_header = promotion.split("    steps:", 1)[0]

    assert "needs: validate-source" in candidate
    assert (
        "candidate-${{ matrix.variant }}-${GITHUB_SHA}-${GITHUB_RUN_ID}-"
        "${GITHUB_RUN_ATTEMPT}" in candidate
    )
    assert "tags: ${{ steps.image.outputs.candidate }}" in candidate
    assert "docker buildx imagetools create" not in candidate
    assert "build-verify-candidate" in promotion
    assert "environment: sandbox-release" in promotion_header
    assert "id-token: write" not in promotion_header
    assert "Reverify all candidates before any alias is changed" in promotion
    assert promotion.index("Reverify all candidates") < promotion.index(
        "Promote immutable and requested moving aliases"
    )
    assert 'tags+=(--tag "${image}:${variant}")' in promotion
    assert 'tags+=(--tag "${image}:latest")' in promotion
    assert 'append_immutable_tag "${variant}-${SHA12}"' in promotion
    assert "Refusing to overwrite immutable sandbox tag" in promotion
    assert 'docker buildx imagetools create "${tags[@]}" "${image}@${digest}"' in promotion


def test_both_published_architectures_are_individually_scanned_and_smoked() -> None:
    workflow = _workflow()

    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert workflow.count("TRIVY_PLATFORM: linux/amd64") == 1
    assert workflow.count("TRIVY_PLATFORM: linux/arm64") == 1
    assert "image-ref: ${{ steps.platforms.outputs.amd64_ref }}" in workflow
    assert "image-ref: ${{ steps.platforms.outputs.arm64_ref }}" in workflow
    assert 'docker run --rm --platform linux/amd64 "${AMD64_REF}"' in workflow
    assert 'docker run --rm --platform linux/arm64 "${ARM64_REF}"' in workflow
    assert '.platform.os == "linux" and .platform.architecture == $architecture' in workflow
    assert workflow.count('.platform_digests | keys == ["linux/amd64", "linux/arm64"]') == 2


def test_security_provenance_and_signature_gates_are_not_best_effort() -> None:
    workflow = _workflow()

    assert "continue-on-error:" not in workflow
    assert "ignore-unfixed: true" not in workflow
    assert workflow.count("ignore-unfixed: false") == 3
    assert workflow.count('exit-code: "1"') == 3
    assert "Attest candidate build provenance" in workflow
    assert "Keyless sign verified candidate digest" in workflow
    assert workflow.count("cosign verify") == 2
    assert workflow.count("gh attestation verify") == 4
    assert "--certificate-identity-regexp" not in workflow
    assert workflow.count('--certificate-identity "${expected_identity}"') == 2
    assert (
        workflow.count(
            'expected_identity="https://github.com/${GITHUB_REPOSITORY}/.github/workflows/'
            'sandbox-image.yml@${GITHUB_REF}"'
        )
        == 2
    )
    assert (
        workflow.count(
            '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/sandbox-image.yml"'
        )
        == 4
    )
    assert workflow.count('--source-ref "${GITHUB_REF}"') == 4
    assert workflow.count('--source-digest "${EXPECTED_SHA}"') == 4
    assert workflow.count("--deny-self-hosted-runners") == 4
    assert workflow.count("--predicate-type https://slsa.dev/provenance/v1") == 2


def test_each_platform_sbom_is_validated_attested_and_exactly_rebound_before_promotion() -> None:
    workflow = _workflow()
    candidate = workflow.split("  build-verify-candidate:", 1)[1].split("  verify-candidates:", 1)[
        0
    ]
    verification = workflow.split("  verify-candidates:", 1)[1].split("  promote:", 1)[0]
    promotion = workflow.split("  promote:", 1)[1]
    promotion_gate = promotion.split(
        "      - name: Promote immutable and requested moving aliases", 1
    )[0]

    assert "artifact-metadata: write" in candidate
    assert "Validate and bind both platform SBOM predicates" in candidate
    assert "Attest linux/amd64 SBOM predicate" in candidate
    assert "Attest linux/arm64 SBOM predicate" in candidate
    assert candidate.count("actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26 # v4.1.0") == 2
    assert "subject-digest: ${{ steps.platforms.outputs.amd64_digest }}" in candidate
    assert "subject-digest: ${{ steps.platforms.outputs.arm64_digest }}" in candidate
    assert "sbom-path: sandbox-sbom/${{ matrix.variant }}-amd64.spdx.json" in candidate
    assert "sbom-path: sandbox-sbom/${{ matrix.variant }}-arm64.spdx.json" in candidate
    assert candidate.count("push-to-registry: true") == 3
    assert '.spdxVersion == "SPDX-2.3"' in candidate
    assert '.SPDXID == "SPDXRef-DOCUMENT"' in candidate
    assert '.dataLicense == "CC0-1.0"' in candidate
    assert "sbom_size > 16777216" in candidate
    assert "outside the GitHub attestation size boundary" in candidate
    assert "schema_version: 2" in candidate
    assert "platform_digests:" in candidate
    assert "sbom_sha256:" in candidate
    assert 'sbom: "attested"' in candidate

    for gate in (verification, promotion_gate):
        assert '--format "{{json (index .SBOM \\"${platform}\\").SPDX}}"' in gate
        assert "--predicate-type https://spdx.dev/Document/v2.3" in gate
        assert "--format json" in gate
        assert "--jq '[.[].verificationResult.statement.predicate]'" in gate
        assert 'jq -e --slurpfile expected "${sbom_path}"' in gate
        assert "any(.[]; . == $expected[0])" in gate
        assert 'test "${observed_platform_digest}" = "${expected_platform_digest}"' in gate
        assert 'test "$(sha256sum "${sbom_path}" | awk' in gate
        assert "verify_platform_sbom" in gate


def test_candidate_records_bind_registry_coordinates_source_run_and_evidence() -> None:
    workflow = _workflow()
    record = workflow.split("      - name: Record verified candidate", 1)[1].split(
        "      - name: Upload verified candidate record", 1
    )[0]
    verification = workflow.split("  verify-candidates:", 1)[1].split("  promote:", 1)[0]
    promotion_gate = workflow.split("      - name: Reverify all candidates", 1)[1].split(
        "      - name: Promote immutable and requested moving aliases", 1
    )[0]

    assert '--arg run_attempt "${GITHUB_RUN_ATTEMPT}"' in record
    assert '--arg run_id "${GITHUB_RUN_ID}"' in record
    assert '--arg source_ref "${GITHUB_REF}"' in record
    assert "run_id: ($run_id | tonumber)" in record
    assert "run_attempt: ($run_attempt | tonumber)" in record
    for gate in (verification, promotion_gate):
        assert ".image == $image" in gate
        assert ".source_ref == $source_ref" in gate
        assert ".source_sha == $source_sha" in gate
        assert ".run_id == ($run_id | tonumber)" in gate
        assert '.run_attempt | type == "number"' in gate
        assert '.candidate == ($image + ":candidate-"' in gate
        assert 'test "$(basename "${record}")" = "${variant}.json"' in gate


def test_docker_toolchains_and_external_images_are_immutable_inputs() -> None:
    dockerfile = _dockerfile()

    assert re.search(r"ARG WOLFI_IMAGE=[^\s]+@sha256:[0-9a-f]{64}", dockerfile)
    assert re.search(r"# syntax=docker/dockerfile:1.7@sha256:[0-9a-f]{64}", dockerfile)
    assert "apk verify APKINDEX.tar.gz" in dockerfile
    assert "xargs apk add --no-network --no-cache" in dockerfile
    assert "diff -u /tmp/expected-apks.lock /tmp/installed-apks.lock" in dockerfile
    assert re.search(r"ARG NODE_VERSION=\d+\.\d+\.\d+", dockerfile)
    assert re.search(r"ARG NODE_LINUX_AMD64_SHA256=[0-9a-f]{64}", dockerfile)
    assert re.search(r"ARG NODE_LINUX_ARM64_SHA256=[0-9a-f]{64}", dockerfile)
    assert "python /opt/npm-rebuild-source/build.py" in dockerfile
    npm_install = dockerfile.split("FROM dev-tools AS dev", 1)[1].split("FROM dev AS server", 1)[0]
    assert npm_install.index('e["archive_sha256"]') < npm_install.index("tar -xzf")
    assert 'test "$(npm --version)" = "11.20.0-alysis.1"' in npm_install
    assert 'test "$(npx --version)" = "11.20.0-alysis.1"' in npm_install
    assert re.search(r"ARG GO_LINUX_AMD64_SHA256=[0-9a-f]{64}", dockerfile)
    assert re.search(r"ARG GO_LINUX_ARM64_SHA256=[0-9a-f]{64}", dockerfile)
    assert re.search(r"ARG RUSTUP_LINUX_AMD64_SHA256=[0-9a-f]{64}", dockerfile)
    assert re.search(r"ARG RUSTUP_LINUX_ARM64_SHA256=[0-9a-f]{64}", dockerfile)
    assert re.search(r"ARG RUST_VERSION=\d+\.\d+\.\d+", dockerfile)
    assert "--network=none --mount=type=bind,from=rust-inputs" in dockerfile
    assert "RUSTUP_DIST_SERVER=file:///opt/rust-inputs" in dockerfile
    assert re.search(r"COPY --from=[^\s]+@sha256:[0-9a-f]{64} /uv", dockerfile)
    assert dockerfile.count("sha256sum -c -") >= 3

    assert "deb.nodesource.com" not in dockerfile
    assert "https://sh.rustup.rs" not in dockerfile
    assert "--default-toolchain stable" not in dockerfile
    assert "npm install -g" not in dockerfile


def test_native_acl_and_filesystem_qualification_are_required_before_promotion() -> None:
    workflow = _workflow()
    verification = workflow.split("  verify-candidates:", 1)[1].split("    steps:", 1)[0]
    assert "- qualify-runtime-boundaries" in verification
    qualification = workflow.split("  qualify-runtime-boundaries:", 1)[1].split(
        "  verify-candidates:", 1
    )[0]
    assert "variant: [base, dev, server]" in qualification
    assert "arch: amd64, runner: ubuntu-latest" in qualification
    assert "arch: arm64, runner: ubuntu-24.04-arm" in qualification
    assert "scripts/sandbox/check_acl_boundary.py" in qualification
    assert "scripts/sandbox/review_image_files.py" in qualification
    assert 'inventory["legacy_acl_callers"] == ["usr/bin/coreutils"]' in qualification
    assert ".run_id == ($run | tonumber)" in qualification
    assert ".source_sha == $sha" in qualification
    assert "continue-on-error" not in qualification


def test_server_image_installs_the_exact_python_lock() -> None:
    dockerfile = _dockerfile()
    dockerignore = DOCKERFILE.with_name("Dockerfile.dockerignore").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock README.md" in dockerfile
    assert "uv sync --locked --no-dev --no-editable --extra server" in dockerfile
    assert "UV_PYTHON_DOWNLOADS=never" in dockerfile
    assert "python -m pip install" not in dockerfile
    assert "!uv.lock" in dockerignore
