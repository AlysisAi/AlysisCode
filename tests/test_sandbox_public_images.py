from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sandbox" / "check-public-images.sh"


@pytest.fixture
def docker_stub(tmp_path: Path):
    if not shutil.which("bash") or not shutil.which("jq") or sys.platform == "win32":
        pytest.skip("public image checks require Bash and jq")
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == '--config'\n"
        "config = pathlib.Path(args[1])\n"
        "with open(os.environ['SANDBOX_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'args': args, 'config': json.loads((config / 'config.json').read_text()), 'env_auth': os.environ.get('DOCKER_AUTH_CONFIG')}) + '\\n')\n"
        "if args[2] == os.environ.get('SANDBOX_TEST_FAIL_COMMAND'):\n"
        "    sys.exit(1)\n"
        "if args[2] == 'manifest':\n"
        "    print(os.environ['SANDBOX_TEST_MANIFEST'])\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    caller_config = tmp_path / "caller-config"
    caller_config.mkdir()
    (caller_config / "config.json").write_text('{"auths": {}}', encoding="utf-8")
    log = tmp_path / "docker-calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CONFIG": str(caller_config),
        "DOCKER_AUTH_CONFIG": '{"auths": {"ghcr.io": {"auth": "test-placeholder"}}}',
        "SANDBOX_TEST_LOG": str(log),
        "SANDBOX_TEST_MANIFEST": json.dumps(
            {
                "manifests": [
                    {"platform": {"os": "linux", "architecture": arch}}
                    for arch in ("amd64", "arm64")
                ]
            }
        ),
    }
    return env, log, caller_config


def test_public_check_uses_empty_credentials_and_pulls_each_platform(docker_stub) -> None:
    env, log, caller_config = docker_stub
    refs = ["ghcr.io/example/sandbox:dev", "ghcr.io/example/sandbox:server"]
    result = subprocess.run(
        ["bash", str(SCRIPT), "--pull", *refs], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(call["config"] == {"auths": {"ghcr.io": {}}} for call in calls)
    assert all(call["env_auth"] is None for call in calls)
    assert all(call["args"][1] != str(caller_config) for call in calls)
    assert (caller_config / "config.json").read_text() == '{"auths": {}}'
    assert [call["args"][2:] for call in calls] == [
        command
        for ref in refs
        for command in (
            ["manifest", "inspect", ref],
            ["pull", "--platform", "linux/amd64", ref],
            ["pull", "--platform", "linux/arm64", ref],
        )
    ]
    assert not Path(calls[0]["args"][1]).exists()


@pytest.mark.parametrize("command", ["manifest", "pull"])
def test_registry_or_layer_download_failure_cannot_report_success(docker_stub, command) -> None:
    env, _, _ = docker_stub
    env["SANDBOX_TEST_FAIL_COMMAND"] = command
    result = subprocess.run(
        ["bash", str(SCRIPT), "--pull", "ghcr.io/example/sandbox:dev"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Public image verified" not in result.stdout


@pytest.mark.parametrize(
    "manifest",
    [
        {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}]},
        {"manifests": [{"platform": {"os": "linux", "architecture": "arm64"}}]},
        {"schemaVersion": 2, "layers": []},
        {},
    ],
)
def test_incomplete_platform_manifest_is_rejected(docker_stub, manifest) -> None:
    env, _, _ = docker_stub
    env["SANDBOX_TEST_MANIFEST"] = json.dumps(manifest)
    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/sandbox:dev"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must contain both" in result.stderr


def test_release_checks_public_access_before_promotion_and_pulls_before_success() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/sandbox-image.yml").read_text())
    jobs = workflow["jobs"]
    assert "verify-candidates" in jobs["promote"]["needs"]
    verification = jobs["verify-candidates"]
    assert verification["permissions"]["packages"] == "read"
    checks = [
        step for step in verification["steps"] if "check-public-images.sh" in step.get("run", "")
    ]
    assert len(checks) == 1
    assert "if" not in checks[0] and "continue-on-error" not in checks[0]
    steps = jobs["promote"]["steps"]
    promotion = next(
        i for i, step in enumerate(steps) if "imagetools create" in step.get("run", "")
    )
    public_pull = next(
        i for i, step in enumerate(steps) if "check-public-images.sh --pull" in step.get("run", "")
    )
    summary = next(i for i, step in enumerate(steps) if step["name"] == "Write promotion summary")
    assert promotion < public_pull < summary
    assert "if" not in steps[public_pull] and "continue-on-error" not in steps[public_pull]


@pytest.mark.parametrize(
    ("rules", "expected_success"),
    [
        (None, False),
        ([], False),
        ([{"type": "wait_timer", "wait_timer": 1}], False),
        ([{"type": "required_reviewers", "prevent_self_review": True, "reviewers": []}], False),
        (
            [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [{"id": 1}],
                }
            ],
            False,
        ),
        (
            [{"type": "required_reviewers", "prevent_self_review": True, "reviewers": [{"id": 1}]}],
            True,
        ),
    ],
)
def test_release_environment_requires_an_independent_reviewer(
    tmp_path: Path, docker_stub, rules, expected_success: bool
) -> None:
    env, _, _ = docker_stub
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "if os.environ.get('SANDBOX_TEST_ENVIRONMENT_FAIL'):\n    sys.exit(1)\n"
        "print(os.environ['SANDBOX_TEST_ENVIRONMENT'])\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env["GITHUB_REPOSITORY"] = "example/sandbox"
    env["SANDBOX_TEST_ENVIRONMENT"] = json.dumps({"protection_rules": rules})
    if rules is None:
        env["SANDBOX_TEST_ENVIRONMENT_FAIL"] = "1"
    workflow = yaml.safe_load((ROOT / ".github/workflows/sandbox-image.yml").read_text())
    job = workflow["jobs"]["validate-source"]
    step = next(
        step for step in job["steps"] if step["name"] == "Check the release approval environment"
    )
    assert "if" not in step and "continue-on-error" not in step
    assert workflow["jobs"]["build-verify-candidate"]["needs"] == "validate-source"
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert (result.returncode == 0) == expected_success


@pytest.mark.parametrize(
    ("ref", "registry"),
    [
        ("alpine:3.22", "https://index.docker.io/v1/"),
        ("library/alpine:3.22", "https://index.docker.io/v1/"),
        ("docker.io/library/alpine:3.22", "https://index.docker.io/v1/"),
        ("index.docker.io/library/alpine:3.22", "https://index.docker.io/v1/"),
        ("registry.example:5000/team/image@sha256:abc", "registry.example:5000"),
        ("localhost:5000/team/image:tag", "localhost:5000"),
        ("localhost/image:tag", "localhost"),
    ],
)
def test_registry_credentials_are_explicitly_empty(docker_stub, ref, registry) -> None:
    env, log, _ = docker_stub
    result = subprocess.run(["bash", str(SCRIPT), ref], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    call = json.loads(log.read_text().splitlines()[0])
    assert call["config"] == {"auths": {registry: {}}}
    assert call["env_auth"] is None
    assert call["args"][2:] == ["manifest", "inspect", ref]
    assert not Path(call["args"][1]).exists()


def test_missing_images_fail_without_calling_docker(docker_stub) -> None:
    env, log, _ = docker_stub
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not log.exists()
