from __future__ import annotations

import os
import subprocess
from pathlib import Path

from alysis_code.git_ops import (
    current_branch,
    ensure_info_exclude_entries,
    ensure_runtime_artifact_excludes,
)


def _cp(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_EDITOR"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["PAGER"] = "cat"
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _git_args(cmd: list[str]) -> list[str]:
    assert cmd[0] == "git"
    assert cmd[1] == "-C"
    args = cmd[3:]
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        cleaned.append(args[i])
        i += 1
    return cleaned


def test_ensure_info_exclude_entries_appends_missing_lines(monkeypatch, tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.write_text("# existing\n/.alysis/\n", encoding="utf-8")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(returncode=0, stdout=str(exclude_path) + "\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = ensure_info_exclude_entries(tmp_path, ["/.alysis/", "/.alysis_images/"])

    assert resolved == exclude_path
    assert exclude_path.read_text(encoding="utf-8").splitlines() == [
        "# existing",
        "/.alysis/",
        "/.alysis_images/",
    ]


def test_ensure_runtime_artifact_excludes_installs_shared_entries(
    monkeypatch, tmp_path: Path
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(returncode=0, stdout=str(exclude_path) + "\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = ensure_runtime_artifact_excludes(tmp_path)

    assert resolved == exclude_path
    assert exclude_path.read_text(encoding="utf-8").splitlines() == [
        "# BEGIN alysis runtime artifacts",
        "/.alysis/",
        "/.alysis_images/",
        "/alysis-feedback/",
        "/.alysis-migration.lock/",
        "__pycache__/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".coverage",
        "*.pyc",
        "*.pyo",
        "# END alysis runtime artifacts",
    ]


def test_info_exclude_writes_use_lf_and_repair_crlf_files(monkeypatch, tmp_path: Path) -> None:
    # Windows text mode used to write "\r\n"; Git reads a CRLF blank line in info/exclude
    # as an empty pattern that matches every path ending in "/".
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.write_bytes(b"# another tool\r\n.kilo/setup-script\r\n\r\n")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(returncode=0, stdout=str(exclude_path) + "\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_runtime_artifact_excludes(tmp_path)
    ensure_info_exclude_entries(tmp_path, ["/extra/"])

    data = exclude_path.read_bytes()
    assert b"\r" not in data
    assert data.startswith(
        b"# another tool\n.kilo/setup-script\n\n# BEGIN alysis runtime artifacts\n"
    )
    assert data.endswith(b"# END alysis runtime artifacts\n/extra/\n")


def test_folders_stay_unignored_after_runtime_excludes_in_a_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_bytes(b"# another tool\r\n*.tmp\r\n\r\n")

    ensure_runtime_artifact_excludes(repo)

    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", "src/"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout


def test_ensure_runtime_artifact_excludes_adds_grounded_rust_target_entries(
    monkeypatch, tmp_path: Path
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    (tmp_path / "crates" / "demo").mkdir(parents=True)
    (tmp_path / "crates" / "demo" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(returncode=0, stdout=str(exclude_path) + "\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_runtime_artifact_excludes(tmp_path)

    assert exclude_path.read_text(encoding="utf-8").splitlines() == [
        "# BEGIN alysis runtime artifacts",
        "/.alysis/",
        "/.alysis_images/",
        "/alysis-feedback/",
        "/.alysis-migration.lock/",
        "__pycache__/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".coverage",
        "*.pyc",
        "*.pyo",
        "/target/",
        "/crates/demo/target/",
        "# END alysis runtime artifacts",
    ]


def test_ensure_runtime_artifact_excludes_removes_legacy_broad_target_entry(
    monkeypatch, tmp_path: Path
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.write_text("target/\n# keep me\n", encoding="utf-8")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(returncode=0, stdout=str(exclude_path) + "\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_runtime_artifact_excludes(tmp_path)

    assert exclude_path.read_text(encoding="utf-8").splitlines() == [
        "# keep me",
        "",
        "# BEGIN alysis runtime artifacts",
        "/.alysis/",
        "/.alysis_images/",
        "/alysis-feedback/",
        "/.alysis-migration.lock/",
        "__pycache__/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".coverage",
        "*.pyc",
        "*.pyo",
        "# END alysis runtime artifacts",
    ]


def test_ensure_runtime_artifact_excludes_does_not_ignore_non_rust_nested_target(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    ensure_runtime_artifact_excludes(tmp_path)

    (tmp_path / "docs" / "target").mkdir(parents=True)
    (tmp_path / "docs" / "target" / "out.txt").write_text("x\n", encoding="utf-8")

    cp = subprocess.run(
        ["git", "-C", os.fspath(tmp_path), "check-ignore", "-v", "docs/target/out.txt"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert cp.returncode == 1


def test_ensure_runtime_artifact_excludes_ignores_grounded_nested_rust_target(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    (tmp_path / "crates" / "demo").mkdir(parents=True)
    (tmp_path / "crates" / "demo" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    ensure_runtime_artifact_excludes(tmp_path)

    (tmp_path / "crates" / "demo" / "target").mkdir(parents=True)
    (tmp_path / "crates" / "demo" / "target" / "out.txt").write_text("x\n", encoding="utf-8")

    cp = subprocess.run(
        [
            "git",
            "-C",
            os.fspath(tmp_path),
            "check-ignore",
            "-v",
            "crates/demo/target/out.txt",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert cp.returncode == 0
    assert "/crates/demo/target/" in cp.stdout


def test_current_branch_returns_symbolic_ref_on_unborn_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    expected = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert expected
    assert current_branch(repo) == expected
