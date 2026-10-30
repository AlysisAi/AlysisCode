from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from scripts.release.smoke_managed_cli import smoke


def test_frozen_smoke_arguments_reach_the_real_stdio_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mocked executable must not hide drift between the smoke harness and CLI."""
    import importlib

    smoke_module = importlib.import_module("scripts.release.smoke_managed_cli")
    executable = tmp_path / "runtime"
    executable.write_bytes(b"cli-contract-fixture")
    run_command = smoke_module._run

    def invoke_cli(
        command: list[str], *, env: dict[str, str], input_text: str | None = None
    ) -> str:
        # The bridge claims private OS descriptors, so exercise real pipes rather
        # than CliRunner's in-memory streams (which do not have file descriptors).
        return run_command(
            [sys.executable, "-m", "alysis_code", *command[1:]],
            env=env,
            input_text=input_text,
        )

    monkeypatch.setattr(smoke_module, "_run", invoke_cli)
    smoke(executable)


def test_managed_cli_workflow_cannot_publish_unsigned_platform_binaries() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "contents: write" not in workflow
    assert "softprops/action-gh-release" not in workflow
    assert "release-assets:" not in workflow
    assert "must not publish release assets" in workflow


def test_signed_upload_keeps_raw_interpreter_receipts_out_of_promotion_inventory(
    tmp_path: Path,
) -> None:
    from scripts.release.build_managed_cli_manifest import TARGETS

    root = tmp_path / "managed-cli-artifacts"
    root.mkdir()
    expected = {"manifest.json"}
    for target in TARGETS:
        executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        expected.update({executable, f"{target}.cdx.json", f"{target}.native-signature.json"})
        (root / f"{target}.python-runtime.json").write_text("{}", encoding="utf-8")
    for name in expected:
        (root / name).write_bytes(b"candidate fixture")
    repository_root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (repository_root / ".github/workflows/managed-cli-vsix-release.yml").read_text(
            encoding="utf-8"
        )
    )
    upload = next(
        step
        for step in workflow["jobs"]["sign-and-assemble"]["steps"]
        if step.get("with", {}).get("name") == "signed-managed-cli-release"
    )
    selected = {
        path.name
        for pattern in upload["with"]["path"].splitlines()
        for path in tmp_path.glob(pattern)
    }
    assert selected == expected
    assert len(selected) == 19


def test_protected_signing_jobs_require_unprivileged_exact_source_validation() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    validation_job = workflow.split("  validate-release-source:", 1)[1].split(
        "  build-managed-cli:", 1
    )[0]
    build_job_header = workflow.split("  build-managed-cli:", 1)[1].split("    strategy:", 1)[0]
    signing_job_header = workflow.split("  sign-and-assemble:", 1)[1].split("    permissions:", 1)[
        0
    ]

    assert "environment:" not in validation_job
    assert "EXPECTED_REPOSITORY: AlysisAi/AlysisCode" in validation_job
    assert 'test "$REPOSITORY_PRIVATE" = "false"' in validation_job
    assert 'test "$REPOSITORY_VISIBILITY" = "public"' in validation_job
    assert 'test "$GITHUB_REF" = "refs/tags/$REQUESTED_TAG"' in validation_job
    assert 'test "$source_commit" = "$GITHUB_SHA"' in validation_job
    assert 'test "$tag_commit" = "$GITHUB_SHA"' in validation_job
    assert 'test "$REQUESTED_TAG" = "v$version"' in validation_job
    assert "validate_github_environment_policy.py" in validation_job
    assert "managed-cli-native-signing" in validation_job
    assert "managed-cli-signing" in validation_job
    assert "--required-pattern 'v*'" in validation_job
    assert 'GH_API_VERSION: "2026-03-10"' in validation_job
    assert "needs: validate-release-source" in build_job_header
    assert "- validate-release-source" in signing_job_header
    assert "- build-managed-cli" in signing_job_header
    assert workflow.index("validate-release-source:") < workflow.index(
        "environment: managed-cli-native-signing"
    )


def test_release_dependency_audits_cannot_be_skipped_in_strict_or_production_mode() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    strict_script = (
        repository_root / "scripts" / "qa" / "check_vscode_extension_release_candidate.sh"
    ).read_text(encoding="utf-8")
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")
    strict_policy = strict_script.split('if [[ "${STRICT_MODE}" == "1" ]]', 1)[1].split(
        'run_root "Validate IDE/CLI parity matrix"', 1
    )[0]

    assert "ALYSIS_RELEASE_SKIP_NPM_CI" in strict_policy
    assert "ALYSIS_RELEASE_SKIP_NPM_AUDIT" in strict_policy
    assert "requires npm ci" in strict_policy
    assert "requires npm audit" in strict_policy
    assert (
        "npm ci && npm run compile && npm run lint && npm test && npm audit --audit-level=high"
    ) in workflow


def test_managed_cli_workflow_verifies_extension_entrypoint_and_excludes_dev_files() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    package = json.loads(
        (repository_root / "extensions/vscode-alysis/package.json").read_text(encoding="utf-8")
    )
    assert f'"extension/{package["main"].removeprefix("./")}"' in workflow
    assert '"extension/out/"' in workflow
    assert '"extension/src/"' in workflow
    assert '"extension/test/"' in workflow
    assert 'name.endswith(".map")' in workflow
    assert (
        'validate_vsix_channel(vsix_manifest, package, channel=os.environ["RELEASE_CHANNEL"])'
        in workflow
    )
    assert "node scripts/package-release.js --target" in workflow


@pytest.mark.parametrize("entrypoint", ["dist/extension.js", "out/src/extension.js"])
def test_managed_vsix_verification_accepts_only_bundled_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github/workflows/managed-cli-vsix-release.yml").read_text(
        encoding="utf-8"
    )
    step = workflow.split("- name: Verify VSIX contains one attested managed runtime", 1)[1]
    embedded = step.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    package = json.loads(
        (repository_root / "extensions/vscode-alysis/package.json").read_text(encoding="utf-8")
    )
    package["version"] = "0.3.0"
    extension_dir = tmp_path / "extensions/vscode-alysis"
    extension_dir.mkdir(parents=True)
    with ZipFile(extension_dir / "vscode-alysis-linux-x64.vsix", "w") as archive:
        archive.writestr("extension/package.json", json.dumps(package))
        archive.writestr(
            "extension.vsixmanifest",
            '<PackageManifest><Property Id="Microsoft.VisualStudio.Code.PreRelease" '
            'Value="true"/></PackageManifest>',
        )
        for name in (
            entrypoint,
            "resources/managed-cli/manifest.json",
            "resources/managed-cli/alysis-linux-x64",
            "resources/managed-cli-release-public.pem",
        ):
            archive.writestr(f"extension/{name}", b"fixture")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET", "linux-x64")
    monkeypatch.setenv("RELEASE_CHANNEL", "beta")
    code = compile(textwrap.dedent(embedded), "verify-vsix-inline.py", "exec")

    if entrypoint == "dist/extension.js":
        exec(code, {})
    else:
        with pytest.raises(SystemExit, match="VSIX missing required runtime entries"):
            exec(code, {})


def test_target_vsix_packaging_uses_one_cross_platform_shell_contract() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    package_step = workflow.split("- name: Package target-specific VSIX", 1)[1].split(
        "- name: Verify VSIX contains one attested managed runtime", 1
    )[0]
    assert "shell: bash" in package_step
    assert 'target "$TARGET"' in package_step


def test_each_target_vsix_is_clean_installed_in_production_mode() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")
    package = json.loads(
        (repository_root / "extensions" / "vscode-alysis" / "package.json").read_text(
            encoding="utf-8"
        )
    )

    assert "test:production-install" in package["scripts"]
    assert "--install-extension" in (
        repository_root
        / "extensions"
        / "vscode-alysis"
        / "test"
        / "integration"
        / "production"
        / "runTest.ts"
    ).read_text(encoding="utf-8")
    assert "Clean-install production VSIX on minimum VS Code" in workflow
    assert "Clean-install production VSIX on current Stable" in workflow
    assert "ALYSIS_PRODUCTION_VSIX_PATH" in workflow
    assert "ALYSIS_NATIVE_SIGNATURE_CHECK: passed" in workflow
    assert "validate_production_dogfood_candidate_binding" in workflow
    assert "production-dogfood-${{ matrix.target }}.json" in workflow
    assert "production-dogfood-minimum-${{ matrix.target }}.json" in workflow
    assert "production-dogfood-current-stable-${{ matrix.target }}.json" in workflow
    assert "VSCODE_TEST_VERSION: 1.90.0" in workflow
    assert "VSCODE_TEST_VERSION: stable" in workflow
    assert 'combined["vscode_compatibility"]' in workflow
    assert "require_vscode_compatibility=True" in workflow
    package_steps = yaml.safe_load(workflow)["jobs"]["package-platform-vsix"]["steps"]
    binding_step = next(
        step
        for step in package_steps
        if step.get("name") == "Validate and bind minimum/current-Stable production evidence"
    )
    # Candidate binding imports cryptography from the frozen verification environment.
    assert binding_step["run"].startswith("uv run --frozen --no-sync python - ")


def test_managed_cli_build_is_locked_attested_and_environment_fenced() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")
    project = (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    ignore = (repository_root / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "pip install --upgrade" not in workflow
    assert (repository_root / "uv.lock").is_file()
    assert "uv.lock" not in {line.strip() for line in ignore}
    assert "uv sync --frozen --no-editable --extra managed-build" in workflow
    assert "pyinstaller==6.21.0" in project
    assert "pyinstaller-hooks-contrib==2026.6" in project
    assert "--format cyclonedx1.5" in workflow
    assert re.search(r"actions/attest@[0-9a-f]{40}\s+#\s+v4\.1\.0", workflow)
    assert "environment: managed-cli-signing" in workflow
    assert "ALYSIS_MANAGED_CLI_PUBLIC_KEY_SHA256" in workflow
    assert "artifact-metadata: write" in workflow
    assert "scripts/release/audit_locked_dependencies.py" in workflow
    assert "manylinux_2_28_x86_64@sha256:" in workflow
    assert "manylinux_2_28_aarch64@sha256:" in workflow
    assert "Prove Linux runtime on the glibc 2.28 baseline" in workflow
    assert "GetCertHashString(" in workflow
    assert "[Security.Cryptography.HashAlgorithmName]::SHA256" in workflow
    assert re.search(r"azure/login@[0-9a-f]{40}\s+#\s+v2", workflow)
    assert re.search(r"azure/artifact-signing-action@[0-9a-f]{40}\s+#\s+v2", workflow)
    assert "client-id: ${{ vars.ALYSIS_AZURE_SIGNING_CLIENT_ID }}" in workflow
    assert (
        "certificate-profile-name: ${{ vars.ALYSIS_AZURE_SIGNING_CERTIFICATE_PROFILE }}" in workflow
    )
    assert "files: ${{ github.workspace }}/managed-cli-out/${{ matrix.executable }}" in workflow
    assert "exclude-environment-credential: true" in workflow
    assert "exclude-azure-cli-credential: false" in workflow
    assert "timestamp-rfc3161: http://timestamp.acs.microsoft.com" in workflow
    assert "scripts/release/verify_windows_runtime.ps1" in workflow
    assert "ALYSIS_WINDOWS_SIGNING_PFX" not in workflow
    assert "client-secret:" not in workflow
    assert "APPLE_NOTARY_ISSUER_ID" in workflow
    assert "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}" in workflow
    assert "BEGIN PRIVATE KEY" in workflow
    assert "Apple notarization response has an invalid submission ID" in workflow
    package_job = workflow.split("package-platform-vsix:", 1)[1]
    assert "ALYSIS_WINDOWS_SIGNING_THUMBPRINT" not in package_job
    assert "$actualIdentity -ne $expectedIdentity" in package_job
    assert (
        '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/managed-cli-vsix-release.yml"'
        in workflow
    )
    assert '--source-ref "refs/tags/$REQUESTED_TAG"' in workflow
    assert '--source-digest "$GITHUB_SHA"' in workflow
    assert "--deny-self-hosted-runners" in workflow


def test_release_candidate_is_blocked_by_committed_activation_performance_budgets() -> None:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")
    performance = workflow.split("  activation-performance:", 1)[1].split(
        "  build-managed-cli:", 1
    )[0]
    package_header = workflow.split("  package-platform-vsix:", 1)[1].split("    runs-on:", 1)[0]

    assert "needs: validate-release-source" in performance
    assert "npm run test:perf-activation" in performance
    assert 'CI: "true"' in performance
    assert "ALYSIS_PERF_LOCAL_OVERRIDE" not in performance
    assert "activation.jsonl" in performance
    assert "if: always()" in performance
    assert "retention-days: 90" in performance
    assert "- activation-performance" in package_header
    assert "- sign-and-assemble" in package_header


def test_native_signing_evidence_is_executable_bound_and_never_injected_into_env() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("--native-evidence ") == 6
    assert "--native-signature " not in workflow
    assert "GITHUB_ENV" not in workflow
    assert "executableSha256" in workflow
    assert "timestampSignerIdentity" in workflow
    assert "native signature evidence does not bind the executable" in workflow
    assert "Developer ID Application: [^\\r\\n]{1,384}" in workflow


def test_final_target_vsix_has_verified_sbom_provenance_before_dogfood() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    build_index = workflow.index("Generate final target VSIX SBOM from verified package bytes")
    provenance_index = workflow.index("Attest final target VSIX provenance")
    sbom_index = workflow.index("Attest final target VSIX SBOM")
    verify_index = workflow.index(
        "Verify final VSIX attestations against immutable release identity"
    )
    install_index = workflow.index("Clean-install production VSIX on minimum VS Code (Linux)")
    assert build_index < provenance_index < sbom_index < verify_index < install_index
    assert "python -m scripts.release.build_vscode_vsix_sbom" in workflow
    assert '--dependency-sbom "managed-cli-release/${TARGET}.cdx.json"' in workflow
    assert "ALYSIS_RELEASE_SIGNATURE_CHECK: passed" in workflow
    assert "vscode-alysis-${{ matrix.target }}.cdx.json" in workflow

    # Execute the workflow's entry point without relying on pytest's import path.
    steps = yaml.safe_load(workflow)["jobs"]["package-platform-vsix"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step.get("name") == "Generate final target VSIX SBOM from verified package bytes"
    )
    arguments = shlex.split(command.splitlines()[0].removesuffix("\\").strip())
    entry_point = arguments[arguments.index("python") + 1 :]
    result = subprocess.run(
        [sys.executable, "-E", *entry_point, "--help"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--dependency-sbom" in result.stdout


def test_only_validated_release_workflow_can_publish_python_distribution() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    legacy = (repository_root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    release = (repository_root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in legacy
    assert "push:" not in legacy
    assert "gh-action-pypi-publish" not in legacy
    assert "contents: write" not in legacy
    assert "legacy workflow cannot publish" in legacy.lower()

    assert 'tags:\n      - "v*"' in release
    assert "Validate tag matches package version" in release
    assert "EXPECTED_REPOSITORY: AlysisAi/AlysisCode" in release
    assert 'test "$REPOSITORY_PRIVATE" = "false"' in release
    assert 'test "$REPOSITORY_VISIBILITY" = "public"' in release
    assert 'test "$source_commit" = "$GITHUB_SHA"' in release
    assert 'test "$tag_commit" = "$GITHUB_SHA"' in release
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in release
    assert 'git check-ref-format --branch "$DEFAULT_BRANCH"' in release
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" "origin/${DEFAULT_BRANCH}"' in release
    assert "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0" in release
    assert 'version: "0.11.23"' in release
    assert "uv sync --frozen --no-editable --extra dev" in release
    assert "uv build --clear --no-create-gitignore --no-build-isolation --no-sources" in release
    assert "scripts/release/validate_python_distributions.py dist/python" in release
    assert "--format cyclonedx1.5" in release
    assert "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d # v4" in release
    assert "gh attestation verify" in release
    assert '--source-ref "$GITHUB_REF"' in release
    assert '--source-digest "$GITHUB_SHA"' in release
    assert "environment: pypi" in release
    assert "pip install" not in release
    assert "pipx" not in release
    assert (
        len(
            re.findall(
                r"pypa/gh-action-pypi-publish@[0-9a-f]{40}\s+#\s+release/v1",
                release,
            )
        )
        == 1
    )
    assert re.search(r"softprops/action-gh-release@[0-9a-f]{40}\s+#\s+v2", release)


def test_ci_python_jobs_use_the_frozen_release_toolchain() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m pip install" not in workflow
    assert "pip install" not in workflow
    assert "pipx" not in workflow
    assert "twine" not in workflow
    assert (
        workflow.count("astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0") == 6
    )
    assert workflow.count('version: "0.11.23"') == 6
    assert "reliability-qualification:" in workflow
    assert "scripts/qa/qualify_production_reliability.py --require-clean" in " ".join(
        workflow.split()
    )
    assert (
        'uv sync --frozen --no-editable --python "${{ matrix.python-version }}" --extra dev'
        in workflow
    )
    jobs = yaml.safe_load(workflow)["jobs"]
    for job_id in ("reliability-qualification", "macos-smoke", "vscode-extension"):
        sync_commands = [
            step["run"]
            for step in jobs[job_id]["steps"]
            if step.get("name", "").startswith("Sync locked")
        ]
        assert len(sync_commands) == 1, job_id
        assert sync_commands[0].startswith(
            'uv sync --frozen --no-editable --managed-python --python "3.12" --extra dev'
        ), job_id
    assert "uv run --frozen --no-sync pytest -q" in workflow
    assert "uv sync --frozen --no-editable --extra dev" in workflow
    assert "uv build --clear --no-create-gitignore --no-build-isolation --no-sources" in workflow
    assert "scripts/release/validate_python_distributions.py dist/python" in workflow
    assert "path: dist/python/*" in workflow


def test_all_external_github_actions_are_commit_pinned_with_version_comments() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    uses_pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)(?:\s+#\s*(.+?))?\s*$")
    pinned_pattern = re.compile(r"[^@\s]+@[0-9a-f]{40}")

    for workflow in sorted((repository_root / ".github" / "workflows").glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = uses_pattern.match(line)
            if match is None:
                continue
            reference, version_comment = match.groups()
            if reference.startswith("./"):
                continue
            if pinned_pattern.fullmatch(reference) is None:
                failures.append(f"{workflow.name}:{line_number}: mutable action ref {reference}")
            if not version_comment:
                failures.append(
                    f"{workflow.name}:{line_number}: pinned action lacks version comment"
                )

    assert failures == []


def test_container_release_uses_job_scoped_github_token_instead_of_a_pat() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github" / "workflows" / "sandbox-image.yml").read_text(
        encoding="utf-8"
    )

    assert "secrets.GHCR_PAT" not in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    expected_permissions = {
        "build-verify-candidate": "write",
        "qualify-runtime-boundaries": "read",
        "promote": "write",
    }
    login_jobs = set()
    for job_id, job in jobs.items():
        for step in job["steps"]:
            if not step.get("uses", "").startswith("docker/login-action@"):
                continue
            login_jobs.add(job_id)
            assert step["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
            assert step["with"]["username"] == "${{ github.actor }}"
            assert job["permissions"]["packages"] == expected_permissions[job_id]
    assert login_jobs == set(expected_permissions)


def test_managed_release_verifies_exact_runtime_and_vsix_sbom_predicates() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("validate_github_attestation_receipt.py") == 4
    assert workflow.count("--expected-predicate") == 2
    assert 'sbom="managed-cli-artifacts/${target}.cdx.json"' in workflow
    assert 'sbom="extensions/vscode-alysis/vscode-alysis-${TARGET}.cdx.json"' in workflow


def test_smoke_checks_version_health_initialize_and_graceful_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "alysis"
    executable.write_bytes(b"fixture")
    calls: list[tuple[list[str], str | None, dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        input_text = kwargs.get("input")
        env = kwargs.get("env")
        assert isinstance(env, dict)
        calls.append((command, input_text if isinstance(input_text, str) else None, env))
        if command[-1] == "--version":
            stdout = "alysis 1.2.3\n"
        elif command[-1] == "health":
            stdout = json.dumps({"ok": True, "protocol_version": "1"})
        else:
            stdout = "\n".join(
                (
                    json.dumps(
                        {
                            "protocol_version": "1",
                            "id": "smoke-init",
                            "result": {"protocol_version": "1"},
                        }
                    ),
                    json.dumps(
                        {
                            "protocol_version": "1",
                            "id": "smoke-shutdown",
                            "result": {"status": "shutting_down"},
                        }
                    ),
                )
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-artifact")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-artifact")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-artifact")
    monkeypatch.setenv("ALYSIS_RELEASE_SIGNING_KEY", "must-not-reach-artifact")
    monkeypatch.setattr(subprocess, "run", fake_run)
    smoke(executable)

    assert [call[0][1:] for call in calls] == [
        ["--version"],
        ["ide-bridge", "health"],
        ["ide-bridge", "--stdio"],
    ]
    assert calls[-1][1] is not None and "bridge.shutdown" in calls[-1][1]
    for _command, _input, env in calls:
        assert "OPENAI_API_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "ALYSIS_RELEASE_SIGNING_KEY" not in env
        assert env["ALYSIS_CONFIG_DIR"]
        assert env["ALYSIS_DATA_DIR"]
