"""Admit publication only after the exact source passed complete CI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode

REQUIRED_JOBS = {
    *(f"test (ubuntu-latest, py{version})" for version in ("3.11", "3.12", "3.13", "3.14")),
    *(
        f"production reliability ({os_name}, py3.12)"
        for os_name in ("ubuntu-latest", "windows-latest", "macos-15")
    ),
    "GitHub workflow lint",
    "package smoke",
}


def validate_ci(run: dict, jobs: list[dict], *, commit: str, branch: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected an independently selected full source commit")
    expected = {
        "head_sha": commit,
        "head_branch": branch,
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
    }
    for key, value in expected.items():
        if run.get(key) != value:
            raise ValueError(f"CI {key} must be {value!r}, got {run.get(key)!r}")
    if run.get("repository", {}).get("full_name") != "AlysisAi/AlysisCode":
        raise ValueError("CI must belong to the official public repository")
    if run.get("head_repository", {}).get("full_name") != "AlysisAi/AlysisCode":
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


def select_run(runs: list[dict], *, commit: str, branch: str) -> int:
    """Select the newest matching run, never fall back past a failed/running run."""
    candidates = [
        run
        for run in runs
        if run.get("head_sha") == commit
        and run.get("head_branch") == branch
        and run.get("event") == "push"
        and run.get("path") == ".github/workflows/ci.yml"
    ]
    if not candidates:
        raise ValueError("No CI push run found for the exact release source and branch")
    return max(run["id"] for run in candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, help="Explicit CI run for publication recovery")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("Expected an independently selected full source commit")

    def api(path: str, *options: str):
        return json.loads(subprocess.check_output(["gh", "api", path, *options], text=True))

    run_id = args.run_id
    if run_id is None:
        query = urlencode(
            {"head_sha": args.commit, "branch": args.branch, "event": "push", "per_page": 100}
        )
        pages = api(
            f"repos/AlysisAi/AlysisCode/actions/workflows/ci.yml/runs?{query}",
            "--paginate",
            "--slurp",
        )
        run_id = select_run(
            [run for page in pages for run in page["workflow_runs"]],
            commit=args.commit,
            branch=args.branch,
        )
    endpoint = f"repos/AlysisAi/AlysisCode/actions/runs/{run_id}"
    run = api(endpoint)
    pages = api(f"{endpoint}/jobs?filter=latest&per_page=100", "--paginate", "--slurp")
    jobs = [job for page in pages for job in page["jobs"]]
    validate_ci(run, jobs, commit=args.commit, branch=args.branch)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"run_id={run_id}\n")
    print(f"Exact-source complete CI verified: {run['html_url']}")


if __name__ == "__main__":
    main()
