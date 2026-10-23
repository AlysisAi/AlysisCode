from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_SHA_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _workflows() -> list[tuple[Path, dict[str, Any]]]:
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(workflow, dict), f"{path} must contain a workflow mapping"
        loaded.append((path, workflow))
    assert loaded, "no GitHub workflows were found"
    return loaded


def test_every_workflow_job_has_a_bounded_timeout() -> None:
    failures: list[str] = []
    for path, workflow in _workflows():
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict) and jobs, f"{path} must define jobs"
        for job_name, job in jobs.items():
            assert isinstance(job, dict), f"{path}:{job_name} must be a mapping"
            timeout = job.get("timeout-minutes")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 360:
                failures.append(f"{path.name}:{job_name}={timeout!r}")

    assert not failures, "jobs without a bounded timeout: " + ", ".join(failures)


def test_checkout_never_persists_the_workflow_token() -> None:
    failures: list[str] = []
    for path, workflow in _workflows():
        for job_name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", []), start=1):
                if not isinstance(step, dict):
                    continue
                if not str(step.get("uses", "")).startswith("actions/checkout@"):
                    continue
                checkout_with = step.get("with")
                if not isinstance(checkout_with, dict) or checkout_with.get(
                    "persist-credentials"
                ) not in (False, "false"):
                    failures.append(f"{path.name}:{job_name}:step-{index}")

    assert not failures, "checkout persists credentials in: " + ", ".join(failures)


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    failures: list[str] = []
    for path, workflow in _workflows():
        for job_name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", []), start=1):
                if not isinstance(step, dict):
                    continue
                action = step.get("uses")
                if not isinstance(action, str) or action.startswith(("./", "docker://")):
                    continue
                if not ACTION_SHA_RE.fullmatch(action):
                    failures.append(f"{path.name}:{job_name}:step-{index}={action}")

    assert not failures, "external actions without full SHA pins: " + ", ".join(failures)


def test_ci_runs_pinned_actionlint_before_packaging() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    lint_job = jobs["workflow-lint"]
    commands = [step.get("run") for step in lint_job["steps"] if "run" in step]

    assert commands == ["go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12"]
    assert "workflow-lint" in jobs["package"]["needs"]


def test_extension_ci_checks_current_stable_before_tagging() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["vscode-extension"]["steps"]
    integration = [step for step in steps if "test:integration" in step.get("run", "")]
    assert len(integration) == 4
    for condition in ("runner.os == 'Linux'", "runner.os != 'Linux'"):
        versions = {
            step.get("env", {}).get("VSCODE_TEST_VERSION", "minimum")
            for step in integration
            if step["if"] == condition
        }
        assert versions == {"minimum", "stable"}


def test_untrusted_context_is_not_interpolated_inside_shell_scripts() -> None:
    failures: list[str] = []
    forbidden = ("${{ inputs.", "${{ github.event.")
    for path, workflow in _workflows():
        for job_name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", []), start=1):
                if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                    continue
                if any(expression in step["run"] for expression in forbidden):
                    failures.append(f"{path.name}:{job_name}:step-{index}")

    assert not failures, (
        "manual/event inputs must enter shell steps through env, not expression interpolation: "
        + ", ".join(failures)
    )
