"""Exercise real workflow stamping/restoration without building a distribution."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.release.validate_python_distributions import _validate_build_stamp

ROOT = Path(__file__).resolve().parents[1]
STAMP = "src/alysis_code/_build_info.py"


@pytest.mark.parametrize("workflow,job", [("release.yml", "build"), ("ci.yml", "package")])
@pytest.mark.parametrize(
    "outcome",
    ["success", "build-failure", "stamp-failure", "termination", "dirty-stamp", "wrong-head"],
)
def test_workflow_stamps_clean_head_and_restores_on_exit(
    tmp_path: Path, workflow: str, job: str, outcome: str
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the disposable release checkout")
    if os.name == "nt":
        # Avoid the WindowsApps bash alias, which opens WSL rather than Git Bash.
        bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if not bash.is_file():
            pytest.skip("Git Bash is required for the workflow shell fixture on Windows")
    else:
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("Bash is required for the release workflow shell fixture")
    steps = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job][
        "steps"
    ]
    build = next(step["run"] for step in steps if "uv build --clear" in step.get("run", ""))
    validation = next(
        step["run"] for step in steps if "validate_python_distributions.py" in step.get("run", "")
    )
    assert '--expected-commit "${{ github.sha }}"' in validation
    assert "--require-clean --no-fetch" in build

    repo = tmp_path / "checkout"
    repo.mkdir()
    for relative in (
        STAMP,
        "scripts/generate_build_info.py",
        "src/alysis_code/__init__.py",
        "src/alysis_code/build_identity.py",
        "src/alysis_code/command_text.py",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    (repo / ".gitignore").write_text("/dist/\n__pycache__/\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2026-10-15T12:00:00Z",
        "GIT_COMMITTER_DATE": "2026-10-15T12:00:00Z",
        "STAMP_TEST_PYTHON": Path(sys.executable).as_posix(),
        "STAMP_TEST_OUTCOME": outcome,
    }

    def run_git(*args: str) -> str:
        return subprocess.run(
            [git, *args], cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=15
        ).stdout.strip()

    run_git("init", "--quiet")
    run_git("add", ".")
    run_git(
        "-c",
        "user.name=Release Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "Release fixture",
    )
    commit = run_git("rev-parse", "HEAD")
    env["GITHUB_SHA"] = "0" * 40 if outcome == "wrong-head" else commit
    if outcome == "dirty-stamp":
        (repo / STAMP).write_bytes(b"# Pre-existing change must survive refusal.\n")
    original = (repo / STAMP).read_bytes()

    # Only uv build is simulated. The exact workflow runs the real generator in
    # its disposable clean checkout, then exposes the stamp the builder sees.
    shim = """
uv() {
  if [ "$1" = "run" ]; then
    shift 4
    "$STAMP_TEST_PYTHON" "$@" || return $?
    if [ "$STAMP_TEST_OUTCOME" = "stamp-failure" ]; then return 37; fi
  elif [ "$1" = "build" ]; then
    mkdir -p dist
    cp src/alysis_code/_build_info.py dist/observed-stamp.py
    if [ "$STAMP_TEST_OUTCOME" = "build-failure" ]; then return 38; fi
    if [ "$STAMP_TEST_OUTCOME" = "termination" ]; then kill -TERM $$; fi
  else
    return 99
  fi
}
"""
    result = subprocess.run(
        [str(bash), "--noprofile", "--norc", "-c", shim + build],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (
        0
        if outcome == "success"
        else {
            "build-failure": 38,
            "stamp-failure": 37,
            "termination": 143,
            "dirty-stamp": 1,
            "wrong-head": 1,
        }[outcome]
    ), result.stdout + result.stderr
    assert (repo / STAMP).read_bytes() == original
    if outcome != "dirty-stamp":
        assert run_git("status", "--porcelain") == ""
    observed = repo / "dist/observed-stamp.py"
    if outcome in {"success", "build-failure", "termination"}:
        source = observed.read_bytes()
        _validate_build_stamp(source, expected_commit=commit)
        assert b'BUILD_TIMESTAMP = "2026-10-15T12:00:00Z"' in source
        assert b'BUILD_RESOLVED_ORIGIN_MAIN = "unavailable"' in source
    else:
        assert not observed.exists()
