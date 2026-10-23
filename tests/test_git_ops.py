from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code.forge import create_plan_run
from alysis_code.git_ops import (
    GitOpsError,
    branch_exists,
    changed_files_between,
    checkout_branch,
    commit_all,
    current_branch,
    ensure_clean_for_pr,
    ensure_git_available,
    ensure_info_exclude_entries,
    ensure_not_staged_prefixes,
    ensure_not_staged_runtime_artifacts,
    ensure_runtime_artifact_excludes,
    format_patch_stdout,
    generate_task_branch_name,
    head_commit,
    merge_no_ff,
    unstage_staged_prefixes,
    unstage_staged_runtime_artifacts,
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


def test_ensure_git_available_errors_when_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    with pytest.raises(GitOpsError, match="git is not available"):
        ensure_git_available()


def test_ensure_clean_for_pr_rejects_untracked_not_ignored(monkeypatch, tmp_path: Path) -> None:
    exclude_path = tmp_path / ".git" / "info" / "exclude"

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(exclude_path) + "\n")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="tmp.txt\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitOpsError, match="untracked files not ignored"):
        ensure_clean_for_pr(tmp_path)


def test_checkout_branch_creates_when_missing(monkeypatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat/t01-demo"]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-demo", "main"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    checkout_branch(tmp_path, "feat/t01-demo", base_branch="main")
    assert ["show-ref", "--verify", "--quiet", "refs/heads/feat/t01-demo"] in seen
    assert ["checkout", "-b", "feat/t01-demo", "main"] in seen


def test_commit_all_uses_deterministic_author(monkeypatch, tmp_path: Path) -> None:
    seen_cmds: list[list[str]] = []
    hooks_dir = tmp_path / "hooks"
    monkeypatch.setenv("ALYSIS_GIT_HOOKS_PATH", str(hooks_dir))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        args = _git_args(cmd)
        if args == ["commit", "-m", "T01: implement feature"]:
            return _cp(returncode=0)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=0, stdout="abc123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_hash = commit_all(tmp_path, message="T01: implement feature")
    assert commit_hash == "abc123"

    commit_cmd = next(cmd for cmd in seen_cmds if "commit" in cmd)
    assert "-c" in commit_cmd
    assert "user.name=alysis-agent" in commit_cmd
    assert "user.email=alysis-agent@local" in commit_cmd
    assert any(str(part).startswith("core.hooksPath=") for part in commit_cmd)


def test_head_commit_returns_hash_or_none(monkeypatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=0, stdout="abc123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert head_commit(tmp_path) == "abc123"
    assert ["rev-parse", "HEAD"] in seen

    def fake_missing(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=128, stderr="fatal: bad revision")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_missing)
    assert head_commit(tmp_path) is None


def test_head_commit_returns_none_on_git_timeout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "interactive-helper")
    monkeypatch.setenv("GCM_INTERACTIVE", "always")
    seen_envs: list[dict[str, str]] = []
    seen_timeouts: list[float] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        seen_envs.append(dict(kwargs["env"]))
        seen_timeouts.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert head_commit(tmp_path) is None
    assert seen_timeouts == [5.0]
    assert seen_envs[0]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen_envs[0]["GIT_ASKPASS"] == ""
    assert seen_envs[0]["SSH_ASKPASS"] == ""
    assert seen_envs[0]["GCM_INTERACTIVE"] == "never"


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


def test_unstage_staged_runtime_artifacts_uses_current_rust_grounding(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    ensure_runtime_artifact_excludes(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "target" / "debug").mkdir(parents=True)
    (tmp_path / "target" / "debug" / "demo").write_text("bin\n", encoding="utf-8")

    _git(tmp_path, "add", "Cargo.toml", "src/main.rs", "target/debug/demo")

    unstaged = unstage_staged_runtime_artifacts(tmp_path)

    assert unstaged == ["target/debug/demo"]
    ensure_not_staged_runtime_artifacts(tmp_path)
    staged = _git(tmp_path, "diff", "--cached", "--name-only").stdout.splitlines()
    assert staged == ["Cargo.toml", "src/main.rs"]


def test_format_patch_stdout_returns_patch_text(monkeypatch, tmp_path: Path) -> None:
    patch = (
        "From 1111111111111111111111111111111111111111 Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] add file\n\n"
        " new file mode 100644\n"
        "---\n"
    )

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(returncode=0, stdout=patch)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = format_patch_stdout(tmp_path, base_branch="main")
    assert "new file mode 100644" in out


def test_commit_all_omits_hooks_override_when_enabled(monkeypatch, tmp_path: Path) -> None:
    seen_cmds: list[list[str]] = []
    monkeypatch.setenv("ALYSIS_GIT_HOOKS", "enable")
    monkeypatch.setenv("ALYSIS_GIT_HOOKS_PATH", str(tmp_path / "hooks"))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        args = _git_args(cmd)
        if args == ["commit", "-m", "T01: implement feature"]:
            return _cp(returncode=0)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=0, stdout="abc123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_hash = commit_all(tmp_path, message="T01: implement feature")
    assert commit_hash == "abc123"

    commit_cmd = next(cmd for cmd in seen_cmds if "commit" in cmd)
    assert all(not str(part).startswith("core.hooksPath=") for part in commit_cmd)
    assert "user.name=alysis-agent" in commit_cmd
    assert "user.email=alysis-agent@local" in commit_cmd


def test_changed_files_between_returns_paths(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(returncode=0, stdout="a.txt\nb/c.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    changed = changed_files_between(tmp_path, revspec="main..HEAD")
    assert changed == ["a.txt", "b/c.py"]


def test_ensure_not_staged_prefixes_blocks_protected_paths(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(returncode=0, stdout=".alysis/current_run.json\nsrc/app.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitOpsError, match="protected path"):
        ensure_not_staged_prefixes(tmp_path, [".alysis", ".alysis_images"])


def test_unstage_staged_prefixes_removes_protected_paths(monkeypatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(returncode=0, stdout="./.alysis/runs/abc/plan.json\nsrc/app.py\n")
        if args == ["reset", "HEAD", "--", "./.alysis/runs/abc/plan.json"]:
            return _cp(returncode=0, stdout="")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    unstaged = unstage_staged_prefixes(tmp_path, [".alysis", ".alysis_images"])
    assert unstaged == ["./.alysis/runs/abc/plan.json"]
    assert ["reset", "HEAD", "--", "./.alysis/runs/abc/plan.json"] in seen


def test_merge_no_ff_returns_merge_commit_hash(monkeypatch, tmp_path: Path) -> None:
    seen_cmds: list[list[str]] = []
    hooks_dir = tmp_path / "hooks"
    monkeypatch.setenv("ALYSIS_GIT_HOOKS_PATH", str(hooks_dir))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        args = _git_args(cmd)
        if args == ["checkout", "main"]:
            return _cp(returncode=0)
        if args == ["merge", "--no-ff", "feat/t01-demo", "-m", "Merge T01: demo"]:
            return _cp(returncode=0)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=0, stdout="merge123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    merge_hash = merge_no_ff(
        tmp_path,
        base_branch="main",
        task_branch="feat/t01-demo",
        message="Merge T01: demo",
    )
    assert merge_hash == "merge123"

    merge_cmd = next(cmd for cmd in seen_cmds if "merge" in cmd)
    assert "user.name=alysis-agent" in merge_cmd
    assert "user.email=alysis-agent@local" in merge_cmd
    assert any(str(part).startswith("core.hooksPath=") for part in merge_cmd)


def test_merge_no_ff_omits_hooks_override_when_enabled(monkeypatch, tmp_path: Path) -> None:
    seen_cmds: list[list[str]] = []
    monkeypatch.setenv("ALYSIS_GIT_HOOKS", "enable")
    monkeypatch.setenv("ALYSIS_GIT_HOOKS_PATH", str(tmp_path / "hooks"))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        args = _git_args(cmd)
        if args == ["checkout", "main"]:
            return _cp(returncode=0)
        if args == ["merge", "--no-ff", "feat/t01-demo", "-m", "Merge T01: demo"]:
            return _cp(returncode=0)
        if args == ["rev-parse", "HEAD"]:
            return _cp(returncode=0, stdout="merge123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    merge_hash = merge_no_ff(
        tmp_path,
        base_branch="main",
        task_branch="feat/t01-demo",
        message="Merge T01: demo",
    )
    assert merge_hash == "merge123"

    merge_cmd = next(cmd for cmd in seen_cmds if "merge" in cmd)
    assert all(not str(part).startswith("core.hooksPath=") for part in merge_cmd)
    assert "user.name=alysis-agent" in merge_cmd
    assert "user.email=alysis-agent@local" in merge_cmd


def test_merge_no_ff_succeeds_without_configured_git_identity(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    global_cfg = tmp_path / "global.gitconfig"
    global_cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.fspath(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", os.fspath(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(tmp_path / "xdg"))

    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")

    local_name = subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "--local", "--get", "user.name"],
        check=False,
        capture_output=True,
        text=True,
    )
    local_email = subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "--local", "--get", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert local_name.returncode != 0
    assert local_email.returncode != 0

    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    base_commit = commit_all(repo, message="init")

    _git(repo, "checkout", "-b", "feat/t01-demo")
    (repo / "README.md").write_text("feature branch\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    feature_commit = commit_all(repo, message="T01: demo")

    merge_hash = merge_no_ff(
        repo,
        base_branch="main",
        task_branch="feat/t01-demo",
        message="Merge T01: demo",
    )

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()

    assert merge_hash == head
    assert len(parents) == 3
    assert set(parents[1:]) == {base_commit, feature_commit}


def test_ensure_clean_for_pr_ignores_framework_and_runtime_artifacts_in_real_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init", "-q")

    create_plan_run(repo)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__pycache__").mkdir()
    (repo / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"pyc")
    (repo / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (repo / ".pytest_cache" / "v" / "cache" / "nodeids").write_text("[]\n", encoding="utf-8")
    (repo / ".ruff_cache" / "0.9.0").mkdir(parents=True)
    (repo / ".ruff_cache" / "0.9.0" / "index").write_text("cache\n", encoding="utf-8")

    ensure_clean_for_pr(repo)

    exclude_path = repo / ".git" / "info" / "exclude"
    entries = set(exclude_path.read_text(encoding="utf-8").splitlines())
    assert "/.alysis/" in entries
    assert "/.alysis_images/" in entries
    assert "__pycache__/" in entries
    assert ".pytest_cache/" in entries
    assert ".ruff_cache/" in entries
    assert ".mypy_cache/" in entries


def test_generate_task_branch_name_slugifies_title() -> None:
    branch = generate_task_branch_name("T01", "Implement: PR flow!")
    assert branch == "feat/t01-implement-pr-flow"


def test_branch_exists_false_on_nonzero(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/x"]:
            return _cp(returncode=1)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert branch_exists(tmp_path, "x") is False


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
