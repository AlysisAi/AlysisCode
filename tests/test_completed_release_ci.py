from __future__ import annotations

import json
import sys
from copy import deepcopy

import pytest

from scripts.release import validate_completed_ci as validator
from scripts.release.validate_completed_ci import REQUIRED_JOBS, select_run, validate_ci

COMMIT = "a" * 40


def candidate():
    run = {
        "id": 42,
        "head_sha": COMMIT,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "AlysisAi/AlysisCode"},
        "head_repository": {"full_name": "AlysisAi/AlysisCode"},
    }
    jobs = [
        {
            "name": name,
            "run_id": 42,
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "success",
        }
        for name in REQUIRED_JOBS
    ]
    return run, jobs


def test_accepts_complete_exact_source_ci():
    run, jobs = candidate()
    validate_ci(run, jobs, commit=COMMIT, branch="main")


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("head_branch", "candidate"),
        ("event", "pull_request"),
        ("path", ".github/workflows/release.yml"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("repository", {"full_name": "fork/AlysisCode"}),
        ("head_repository", {"full_name": "fork/AlysisCode"}),
    ],
)
def test_rejects_wrong_source_or_nonpassing_run(field, value):
    run, jobs = candidate()
    run[field] = value
    with pytest.raises(ValueError):
        validate_ci(run, jobs, commit=COMMIT, branch="main")


@pytest.mark.parametrize("name", sorted(REQUIRED_JOBS))
@pytest.mark.parametrize(
    "mutation", ["missing", "skipped", "wrong-commit", "wrong-run", "duplicate"]
)
def test_every_required_job_must_pass_at_exact_source(name, mutation):
    run, jobs = candidate()
    job = next(job for job in jobs if job["name"] == name)
    if mutation == "missing":
        jobs.remove(job)
    elif mutation == "skipped":
        job["conclusion"] = "skipped"
    elif mutation == "wrong-commit":
        job["head_sha"] = "b" * 40
    elif mutation == "wrong-run":
        job["run_id"] = 43
    else:
        jobs.append(deepcopy(job))
    with pytest.raises(ValueError):
        validate_ci(run, jobs, commit=COMMIT, branch="main")


def test_rejects_abbreviated_source_commit():
    run, jobs = candidate()
    with pytest.raises(ValueError):
        validate_ci(run, jobs, commit=COMMIT[:12], branch="main")


@pytest.mark.parametrize("run_id", [None, 42])
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_cli_records_only_verified_ci_and_supports_discovery(
    monkeypatch, tmp_path, run_id, conclusion
):
    run, jobs = candidate()
    run.update(
        conclusion=conclusion, html_url="https://github.com/AlysisAi/AlysisCode/actions/runs/42"
    )
    calls = []

    def api(command, *, text):
        calls.append(command)
        path = command[2]
        if "/workflows/ci.yml/runs?" in path:
            assert "head_sha=" + COMMIT in path
            assert "branch=main" in path and "event=push" in path
            assert command[3:] == ["--paginate", "--slurp"]
            return json.dumps([{"workflow_runs": []}, {"workflow_runs": [run]}])
        if "/jobs?" in path:
            assert "filter=latest" in path
            return json.dumps([{"jobs": jobs[:4]}, {"jobs": jobs[4:]}])
        assert path.endswith("/runs/42")
        return json.dumps(run)

    output = tmp_path / "github-output"
    argv = ["validator", "--commit", COMMIT, "--branch", "main", "--github-output", str(output)]
    if run_id is not None:
        argv += ["--run-id", str(run_id)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(validator.subprocess, "check_output", api)
    if conclusion == "success":
        validator.main()
        assert output.read_text() == "run_id=42\n"
    else:
        with pytest.raises(ValueError, match="conclusion"):
            validator.main()
        assert not output.exists()
    assert len(calls) == (3 if run_id is None else 2)


@pytest.mark.parametrize("conclusion,status", [("failure", "completed"), (None, "in_progress")])
def test_discovery_never_falls_back_to_older_success(conclusion, status):
    run, jobs = candidate()
    newer = {**run, "id": 43, "conclusion": conclusion, "status": status}
    assert select_run([newer, run], commit=COMMIT, branch="main") == 43
    with pytest.raises(ValueError):
        validate_ci(newer, jobs, commit=COMMIT, branch="main")


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("head_branch", "other"),
        ("event", "pull_request"),
        ("path", ".github/workflows/release.yml"),
    ],
)
def test_discovery_rejects_runs_from_other_sources(field, value):
    run, _ = candidate()
    run[field] = value
    with pytest.raises(ValueError, match="No CI push run"):
        select_run([run], commit=COMMIT, branch="main")


def test_discovery_rejects_no_runs():
    with pytest.raises(ValueError, match="No CI push run"):
        select_run([], commit=COMMIT, branch="main")
