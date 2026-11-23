from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

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
        assert output.read_text() == f"run_id=42\ntested_commit={COMMIT}\n"
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


# The release pull request's CI may stand in for the release commit's own push run.

HEAD = "c" * 40
TREE = "d" * 40
MAIN_BEFORE = "e" * 40
HEAD_BRANCH = "publish/v1.2.3"


def release_pull(**changes):
    pull = {
        "number": 7,
        "merged_at": "2026-11-23T10:00:00Z",
        "merge_commit_sha": COMMIT,
        "base": {"ref": "main", "repo": {"full_name": "AlysisAi/AlysisCode"}},
        "head": {
            "sha": HEAD,
            "ref": HEAD_BRANCH,
            "repo": {"full_name": "AlysisAi/AlysisCode"},
        },
    }
    for key, value in changes.items():
        section, _, field = key.partition("__")
        if field:
            pull[section] = {**pull[section], field: value}
        else:
            pull[section] = value
    return pull


def pull_request_candidate(run_id=50):
    run, jobs = candidate()
    run.update(id=run_id, head_sha=HEAD, head_branch=HEAD_BRANCH, event="pull_request")
    for job in jobs:
        job.update(run_id=run_id, head_sha=HEAD)
    return run, jobs


def merge_commits(*, release_tree=TREE, head_tree=TREE, parents=(MAIN_BEFORE, HEAD)):
    return (
        {"sha": COMMIT, "tree": {"sha": release_tree}, "parents": [{"sha": p} for p in parents]},
        {"sha": HEAD, "tree": {"sha": head_tree}, "parents": [{"sha": MAIN_BEFORE}]},
    )


class FakeGitHub:
    """Answers the validator's `gh api` calls from a recorded repository state."""

    def __init__(self):
        self.push_runs = []
        self.pull_request_runs = []
        self.jobs = {}
        self.runs = {}
        self.pulls = [release_pull()]
        release, head = merge_commits()
        self.commits = {COMMIT: release, HEAD: head}
        self.comparisons = {f"{MAIN_BEFORE}...{HEAD}": {"status": "ahead", "behind_by": 0}}
        self.paths = []

    def add(self, run, jobs, *, kind):
        (self.push_runs if kind == "push" else self.pull_request_runs).append(run)
        self.runs[run["id"]] = {**run, "html_url": f"https://example.invalid/runs/{run['id']}"}
        self.jobs[run["id"]] = jobs

    def __call__(self, command, *, text):
        assert command[:2] == ["gh", "api"] and text
        path = command[2]
        self.paths.append(path)
        repository = "repos/AlysisAi/AlysisCode/"
        assert path.startswith(repository)
        path = path[len(repository) :]
        if path.startswith("actions/workflows/ci.yml/runs?"):
            assert command[3:] == ["--paginate", "--slurp"]
            if "event=push" in path:
                assert f"head_sha={COMMIT}" in path and "branch=main" in path
                return json.dumps([{"workflow_runs": self.push_runs}])
            assert "event=pull_request" in path and f"head_sha={HEAD}" in path
            assert "branch=publish%2Fv1.2.3" in path
            return json.dumps([{"workflow_runs": self.pull_request_runs}])
        if path.startswith("actions/runs/") and "/jobs?" in path:
            run_id = int(path.split("/")[2])
            return json.dumps([{"jobs": self.jobs[run_id]}])
        if path.startswith("actions/runs/"):
            return json.dumps(self.runs[int(path.split("/")[2])])
        if path.startswith(f"commits/{COMMIT}/pulls"):
            return json.dumps(self.pulls)
        if path.startswith("git/commits/"):
            return json.dumps(self.commits[path.split("/")[2]])
        if path.startswith("compare/"):
            return json.dumps(self.comparisons[path.split("/")[1].split("?")[0]])
        raise AssertionError(f"unexpected API call {path}")


def run_validator(monkeypatch, tmp_path, github, *, accept=True, run_id=None):
    output = tmp_path / "github-output"
    argv = ["validator", "--commit", COMMIT, "--branch", "main", "--github-output", str(output)]
    if accept:
        argv.append("--accept-release-pull-request")
    if run_id is not None:
        argv += ["--run-id", str(run_id)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(validator.subprocess, "check_output", github)
    validator.main()
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def running_push_run():
    run, jobs = candidate()
    run.update(status="in_progress", conclusion=None)
    for job in jobs:
        job.update(status="in_progress", conclusion=None)
    return run, jobs


@pytest.mark.parametrize("main_run", ["running", "missing"])
def test_release_pull_request_ci_stands_in_while_main_ci_has_no_verdict(
    monkeypatch, tmp_path, main_run
):
    github = FakeGitHub()
    if main_run == "running":
        github.add(*running_push_run(), kind="push")
    github.add(*pull_request_candidate(), kind="pull_request")
    assert run_validator(monkeypatch, tmp_path, github) == {
        "run_id": "50",
        "tested_commit": HEAD,
    }


def test_squash_merged_release_pull_request_with_the_same_tree_is_accepted(monkeypatch, tmp_path):
    github = FakeGitHub()
    release, _ = merge_commits(parents=(MAIN_BEFORE,))
    github.commits[COMMIT] = release
    github.add(*pull_request_candidate(), kind="pull_request")
    assert run_validator(monkeypatch, tmp_path, github)["tested_commit"] == HEAD


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
def test_finished_main_ci_always_decides(monkeypatch, tmp_path, conclusion):
    github = FakeGitHub()
    run, jobs = candidate()
    run["conclusion"] = conclusion
    github.add(run, jobs, kind="push")
    github.add(*pull_request_candidate(), kind="pull_request")
    if conclusion == "success":
        assert run_validator(monkeypatch, tmp_path, github) == {
            "run_id": "42",
            "tested_commit": COMMIT,
        }
    else:
        with pytest.raises(ValueError, match="own CI run has finished"):
            run_validator(monkeypatch, tmp_path, github)


def refuse_pull_request(github, monkeypatch, tmp_path, match, **kwargs):
    github.add(*running_push_run(), kind="push")
    with pytest.raises(ValueError, match=match) as refusal:
        run_validator(monkeypatch, tmp_path, github, **kwargs)
    # Today's rule still applies: the release commit's own CI must have finished.
    assert "CI status must be 'completed'" in str(refusal.value)
    assert not (tmp_path / "github-output").exists()


def test_different_files_than_the_pull_request_head_fall_back_to_main_ci(monkeypatch, tmp_path):
    github = FakeGitHub()
    release, _ = merge_commits(release_tree="f" * 40)
    github.commits[COMMIT] = release
    github.add(*pull_request_candidate(), kind="pull_request")
    refuse_pull_request(github, monkeypatch, tmp_path, "files differ")


@pytest.mark.parametrize(
    "comparison", [{"status": "diverged", "behind_by": 1}, {"status": "behind", "behind_by": 2}]
)
def test_main_changes_missing_from_the_head_fall_back_even_with_the_same_tree(
    monkeypatch, tmp_path, comparison
):
    # The pull request's CI tested its head merged into a base it did not contain.
    github = FakeGitHub()
    github.comparisons[f"{MAIN_BEFORE}...{HEAD}"] = comparison
    github.add(*pull_request_candidate(), kind="pull_request")
    refuse_pull_request(github, monkeypatch, tmp_path, "did not contain")


@pytest.mark.parametrize(
    "mutation", ["failed", "running", "missing-job", "skipped-job", "other-head", "fork"]
)
def test_pull_request_ci_must_be_a_complete_success_on_that_head(monkeypatch, tmp_path, mutation):
    github = FakeGitHub()
    run, jobs = pull_request_candidate()
    if mutation == "failed":
        run["conclusion"] = "failure"
    elif mutation == "running":
        run.update(status="in_progress", conclusion=None)
    elif mutation == "missing-job":
        jobs.pop()
    elif mutation == "skipped-job":
        jobs[0]["conclusion"] = "skipped"
    elif mutation == "other-head":
        for job in jobs:
            job["head_sha"] = "b" * 40
    else:
        run["head_repository"] = {"full_name": "fork/AlysisCode"}
    github.add(run, jobs, kind="pull_request")
    refuse_pull_request(github, monkeypatch, tmp_path, "release pull request's CI")


def test_newest_pull_request_run_decides_never_an_older_success(monkeypatch, tmp_path):
    github = FakeGitHub()
    github.add(*pull_request_candidate(50), kind="pull_request")
    newer, jobs = pull_request_candidate(51)
    newer["conclusion"] = "failure"
    github.add(newer, jobs, kind="pull_request")
    refuse_pull_request(github, monkeypatch, tmp_path, "conclusion")


@pytest.mark.parametrize(
    "pulls",
    [
        [],
        [release_pull(merged_at=None)],
        [release_pull(merge_commit_sha="b" * 40)],
        [release_pull(base__ref="release")],
        [release_pull(head__repo={"full_name": "fork/AlysisCode"})],
        [release_pull(head__repo=None)],
        [release_pull(), release_pull(number=8)],
    ],
)
def test_only_the_one_official_merged_release_pull_request_counts(monkeypatch, tmp_path, pulls):
    github = FakeGitHub()
    github.pulls = pulls
    github.add(*pull_request_candidate(), kind="pull_request")
    refuse_pull_request(github, monkeypatch, tmp_path, "merged pull request")


def test_pull_request_ci_counts_only_when_the_release_workflow_asks(monkeypatch, tmp_path):
    github = FakeGitHub()
    github.add(*pull_request_candidate(), kind="pull_request")
    github.add(*running_push_run(), kind="push")
    with pytest.raises(ValueError, match="status must be 'completed'"):
        run_validator(monkeypatch, tmp_path, github, accept=False)
    assert not any("pulls" in path for path in github.paths)


@pytest.mark.parametrize("accept", [True, False])
def test_explicit_recovery_run_may_name_the_release_pull_request_run(monkeypatch, tmp_path, accept):
    github = FakeGitHub()
    github.add(*pull_request_candidate(), kind="pull_request")
    if accept:
        assert run_validator(monkeypatch, tmp_path, github, run_id=50) == {
            "run_id": "50",
            "tested_commit": HEAD,
        }
    else:
        with pytest.raises(ValueError, match="CI head_sha must be"):
            run_validator(monkeypatch, tmp_path, github, accept=False, run_id=50)


def test_explicit_pull_request_run_never_overrides_a_failed_main_run(monkeypatch, tmp_path):
    github = FakeGitHub()
    failed, jobs = candidate()
    failed["conclusion"] = "failure"
    github.add(failed, jobs, kind="push")
    github.add(*pull_request_candidate(), kind="pull_request")
    with pytest.raises(ValueError, match="own CI run has finished"):
        run_validator(monkeypatch, tmp_path, github, run_id=50)


def test_explicit_push_run_keeps_todays_recovery(monkeypatch, tmp_path):
    github = FakeGitHub()
    github.add(*candidate(), kind="push")
    assert run_validator(monkeypatch, tmp_path, github, run_id=42) == {
        "run_id": "42",
        "tested_commit": COMMIT,
    }
    assert not any("pulls" in path for path in github.paths)


def test_same_source_compares_trees_not_commits():
    release, head = merge_commits()
    assert release["sha"] != head["sha"]
    comparisons = {MAIN_BEFORE: {"status": "ahead", "behind_by": 0}}
    validator.validate_same_source(release, head, comparisons, commit=COMMIT, head_commit=HEAD)
    _, changed = merge_commits(head_tree="f" * 40)
    with pytest.raises(ValueError, match="files differ"):
        validator.validate_same_source(
            release, changed, comparisons, commit=COMMIT, head_commit=HEAD
        )
    with pytest.raises(ValueError, match="do not belong"):
        validator.validate_same_source(head, head, comparisons, commit=COMMIT, head_commit=HEAD)
    with pytest.raises(ValueError, match="no parent"):
        validator.validate_same_source(
            {**release, "parents": []}, head, comparisons, commit=COMMIT, head_commit=HEAD
        )


def test_release_workflow_accepts_the_release_pull_request_and_records_what_ci_tested():
    root = Path(__file__).resolve().parents[1]
    release = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = release["jobs"]
    validation = jobs["validate-release-source"]
    step = next(step for step in validation["steps"] if step.get("id") == "completed-ci")
    assert "--accept-release-pull-request" in step["run"]
    assert "if" not in step and not step.get("continue-on-error", False)
    assert validation["outputs"]["ci_commit"] == "${{ steps.completed-ci.outputs.tested_commit }}"
    tested = "${{ needs.validate-release-source.outputs.ci_commit }}"
    assert jobs["build"]["env"]["CI_COMMIT"] == tested
    record = next(
        step
        for step in jobs["build"]["steps"]
        if step.get("name") == "Record the immutable release source"
    )
    assert '"CI_COMMIT"' in record["run"]
    verify = next(
        step
        for step in jobs["publish-pypi"]["steps"]
        if step.get("name") == "Verify source-bound provenance before publishing"
    )
    assert verify["env"]["CI_COMMIT"] == tested
    assert '"CI_COMMIT"' in verify["run"]
