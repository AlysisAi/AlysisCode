"""Admit publication recovery only after the exact source passed complete CI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    endpoint = f"repos/AlysisAi/AlysisCode/actions/runs/{args.run_id}"

    def api(path: str, *options: str):
        return json.loads(subprocess.check_output(["gh", "api", path, *options], text=True))

    run = api(endpoint)
    pages = api(f"{endpoint}/jobs?filter=latest&per_page=100", "--paginate", "--slurp")
    jobs = [job for page in pages for job in page["jobs"]]
    validate_ci(run, jobs, commit=args.commit, branch=args.branch)
    print(f"Exact-source complete CI verified: {run['html_url']}")


if __name__ == "__main__":
    main()
