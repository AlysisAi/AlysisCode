"""Admit publication only after the exact source passed complete CI.

The release commit's own CI push run on the default branch is the ordinary proof.
With --accept-release-pull-request, the CI run of the merged release pull request can
stand in for it while that push run has no verdict yet, but only when the CI run
tested exactly the release commit's files:

- GitHub records one merged pull request, from the official repository into the
  default branch, whose merge produced the release commit;
- the release commit's tree is that pull request's head tree (trees, not commits);
- every other parent of the release commit is already in the pull request head, so
  the pull request's CI, which tests the head merged into its base, saw the same tree;
- the newest CI run for that head is a complete success with every required job.

A completed push run for the release commit always decides: a failed one is never
overridden by the pull request's run. Anything else falls back to the push run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode

OFFICIAL_REPOSITORY = "AlysisAi/AlysisCode"
CI_WORKFLOW = ".github/workflows/ci.yml"
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")

REQUIRED_JOBS = {
    *(f"test (ubuntu-latest, py{version})" for version in ("3.11", "3.12", "3.13", "3.14")),
    *(
        f"production reliability ({os_name}, py3.12)"
        for os_name in ("ubuntu-latest", "windows-latest", "macos-15")
    ),
    "GitHub workflow lint",
    "package smoke",
}


def validate_ci(
    run: dict, jobs: list[dict], *, commit: str, branch: str, event: str = "push"
) -> None:
    if not FULL_COMMIT.fullmatch(commit):
        raise ValueError("Expected an independently selected full source commit")
    expected = {
        "head_sha": commit,
        "head_branch": branch,
        "event": event,
        "path": CI_WORKFLOW,
        "status": "completed",
        "conclusion": "success",
    }
    for key, value in expected.items():
        if run.get(key) != value:
            raise ValueError(f"CI {key} must be {value!r}, got {run.get(key)!r}")
    if run.get("repository", {}).get("full_name") != OFFICIAL_REPOSITORY:
        raise ValueError("CI must belong to the official public repository")
    if run.get("head_repository", {}).get("full_name") != OFFICIAL_REPOSITORY:
        raise ValueError("CI source must belong to the official public repository")
    for name in REQUIRED_JOBS:
        matching = [job for job in jobs if job.get("name") == name]
        if len(matching) != 1:
            raise ValueError(f"Expected exactly one required CI job: {name}")
        job = matching[0]
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("head_sha") != commit
            or job.get("run_id") != run.get("id")
        ):
            raise ValueError(f"Required CI job did not pass for the exact source: {name}")


def _matching_runs(runs: list[dict], *, commit: str, branch: str, event: str) -> list[dict]:
    return [
        run
        for run in runs
        if run.get("head_sha") == commit
        and run.get("head_branch") == branch
        and run.get("event") == event
        and run.get("path") == CI_WORKFLOW
    ]


def select_run(runs: list[dict], *, commit: str, branch: str, event: str = "push") -> int:
    """Select the newest matching run, never fall back past a failed/running run."""
    candidates = _matching_runs(runs, commit=commit, branch=branch, event=event)
    if not candidates:
        kind = "push" if event == "push" else event.replace("_", " ")
        raise ValueError(f"No CI {kind} run found for the exact release source and branch")
    return max(run["id"] for run in candidates)


def newest_run(runs: list[dict], *, commit: str, branch: str) -> dict | None:
    """The newest push run for the release commit, if there is one."""
    candidates = _matching_runs(runs, commit=commit, branch=branch, event="push")
    return max(candidates, key=lambda run: run["id"]) if candidates else None


def release_pull_request(pulls: list[dict], *, commit: str, branch: str) -> dict:
    """The one merged official pull request whose merge produced the release commit."""
    matching = [
        pull
        for pull in pulls
        if pull.get("merged_at")
        and pull.get("merge_commit_sha") == commit
        and pull.get("base", {}).get("ref") == branch
        and (pull.get("base", {}).get("repo") or {}).get("full_name") == OFFICIAL_REPOSITORY
        and (pull.get("head", {}).get("repo") or {}).get("full_name") == OFFICIAL_REPOSITORY
    ]
    if len(matching) != 1:
        raise ValueError(
            "Expected exactly one merged pull request from the official repository "
            f"whose merge is the release commit, found {len(matching)}"
        )
    head = matching[0]["head"]
    if not FULL_COMMIT.fullmatch(str(head.get("sha", ""))) or not head.get("ref"):
        raise ValueError("The release pull request has no full head commit and branch")
    return matching[0]


def validate_same_source(
    release: dict, head: dict, comparisons: dict[str, dict], *, commit: str, head_commit: str
) -> None:
    """Prove the pull request's CI tested exactly the release commit's files."""
    if release.get("sha") != commit or head.get("sha") != head_commit:
        raise ValueError("Commit details do not belong to the release and pull request head")
    release_tree = release.get("tree", {}).get("sha")
    head_tree = head.get("tree", {}).get("sha")
    if not release_tree or not FULL_COMMIT.fullmatch(release_tree):
        raise ValueError("The release commit has no full tree")
    if release_tree != head_tree:
        raise ValueError(
            "The release commit's files differ from the release pull request head "
            f"(tree {release_tree} is not {head_tree})"
        )
    parents = [parent.get("sha") for parent in release.get("parents", [])]
    if not parents:
        raise ValueError("The release commit has no parent")
    for parent in parents:
        if parent == head_commit:
            continue
        comparison = comparisons.get(parent, {})
        if (
            comparison.get("status") not in {"ahead", "identical"}
            or comparison.get("behind_by") != 0
        ):
            raise ValueError(
                f"The default branch had changes ({parent}) that the release pull request "
                "head did not contain, so its CI did not test the merged tree"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, help="Explicit CI run for publication recovery")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument(
        "--accept-release-pull-request",
        action="store_true",
        help="Accept the merged release pull request's CI run for the same tree",
    )
    args = parser.parse_args()
    if not FULL_COMMIT.fullmatch(args.commit):
        parser.error("Expected an independently selected full source commit")

    repository = f"repos/{OFFICIAL_REPOSITORY}"

    def api(path: str, *options: str):
        return json.loads(subprocess.check_output(["gh", "api", path, *options], text=True))

    def workflow_runs(**query: str | int) -> list[dict]:
        pages = api(
            f"{repository}/actions/workflows/ci.yml/runs?{urlencode({**query, 'per_page': 100})}",
            "--paginate",
            "--slurp",
        )
        return [run for page in pages for run in page["workflow_runs"]]

    def run_and_jobs(run_id: int) -> tuple[dict, list[dict]]:
        endpoint = f"{repository}/actions/runs/{run_id}"
        run = api(endpoint)
        pages = api(f"{endpoint}/jobs?filter=latest&per_page=100", "--paginate", "--slurp")
        return run, [job for page in pages for job in page["jobs"]]

    found_push_runs: list[list[dict]] = []

    def push_runs() -> list[dict]:
        if not found_push_runs:
            found_push_runs.append(
                workflow_runs(head_sha=args.commit, branch=args.branch, event="push")
            )
        return found_push_runs[0]

    def release_head() -> tuple[str, str]:
        """The release pull request head whose tree the release commit has."""
        pulls = api(f"{repository}/commits/{args.commit}/pulls?per_page=100")
        pull = release_pull_request(pulls, commit=args.commit, branch=args.branch)
        head_commit, head_branch = pull["head"]["sha"], pull["head"]["ref"]
        release = api(f"{repository}/git/commits/{args.commit}")
        head = api(f"{repository}/git/commits/{head_commit}")
        comparisons = {
            parent["sha"]: api(f"{repository}/compare/{parent['sha']}...{head_commit}?per_page=1")
            for parent in release.get("parents", [])
            if parent.get("sha") != head_commit
        }
        validate_same_source(
            release, head, comparisons, commit=args.commit, head_commit=head_commit
        )
        return head_commit, head_branch

    def require_no_completed_push_verdict() -> None:
        run = newest_run(push_runs(), commit=args.commit, branch=args.branch)
        if run is not None and run.get("status") == "completed":
            raise ValueError(
                "The release commit's own CI run has finished; it decides "
                f"(conclusion {run.get('conclusion')!r})"
            )

    def pull_request_ci(run_id: int | None) -> tuple[int, dict, str]:
        head_commit, head_branch = release_head()
        if run_id is None:
            runs = workflow_runs(head_sha=head_commit, branch=head_branch, event="pull_request")
            run_id = select_run(runs, commit=head_commit, branch=head_branch, event="pull_request")
        run, jobs = run_and_jobs(run_id)
        validate_ci(run, jobs, commit=head_commit, branch=head_branch, event="pull_request")
        return run_id, run, head_commit

    def push_ci(run_id: int | None) -> tuple[int, dict, str]:
        if run_id is None:
            run_id = select_run(push_runs(), commit=args.commit, branch=args.branch)
        run, jobs = run_and_jobs(run_id)
        validate_ci(run, jobs, commit=args.commit, branch=args.branch)
        return run_id, run, args.commit

    pull_request_refusal = None
    verified = None
    if args.accept_release_pull_request:
        try:
            if args.run_id is None:
                require_no_completed_push_verdict()
                verified = pull_request_ci(None)
            elif api(f"{repository}/actions/runs/{args.run_id}").get("event") == "pull_request":
                require_no_completed_push_verdict()
                verified = pull_request_ci(args.run_id)
        except ValueError as error:
            pull_request_refusal = error
    if verified is None:
        try:
            verified = push_ci(args.run_id)
        except ValueError as error:
            if pull_request_refusal is None:
                raise
            raise ValueError(
                f"{error}. The release pull request's CI was not accepted instead: "
                f"{pull_request_refusal}"
            ) from error
    run_id, run, tested_commit = verified
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"run_id={run_id}\n")
            output.write(f"tested_commit={tested_commit}\n")
    if tested_commit == args.commit:
        print(f"Exact-source complete CI verified: {run['html_url']}")
    else:
        print(
            "Release pull request CI verified for the same tree "
            f"(head {tested_commit}): {run['html_url']}"
        )


if __name__ == "__main__":
    main()
