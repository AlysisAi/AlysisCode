from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.release.validate_completed_ci import REQUIRED_JOBS, validate_ci

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
