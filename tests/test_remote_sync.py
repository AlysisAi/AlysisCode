from __future__ import annotations

import subprocess
from pathlib import Path

from alysis_code.remote_sync import (
    create_pr_or_mr,
    detect_provider_from_remote_url,
    ensure_pr_or_mr,
    find_existing_pr_or_mr,
    get_remote_url,
    load_remote_settings_from_env,
    push_base,
    push_branch,
    resolve_provider,
)


def _cp(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_load_remote_settings_defaults_off() -> None:
    settings = load_remote_settings_from_env({})
    assert settings.sync_mode == "off"
    assert settings.remote_name == "origin"
    assert settings.create_pr is False
    assert settings.provider == "auto"
    assert settings.enabled is False


def test_load_remote_settings_honors_canonical_environment_names() -> None:
    settings = load_remote_settings_from_env(
        {
            "ALYSIS_REMOTE_SYNC": "strict",
            "ALYSIS_REMOTE_NAME": "upstream",
            "ALYSIS_REMOTE_CREATE_PR": "1",
            "ALYSIS_REMOTE_PROVIDER": "github",
        }
    )

    assert settings.sync_mode == "strict"
    assert settings.remote_name == "upstream"
    assert settings.create_pr is True
    assert settings.provider == "github"


def test_detect_provider_from_remote_urls() -> None:
    assert detect_provider_from_remote_url("git@github.com:org/repo.git") == "github"
    assert detect_provider_from_remote_url("https://github.com/org/repo.git") == "github"
    assert detect_provider_from_remote_url("git@gitlab.com:org/repo.git") == "gitlab"
    assert detect_provider_from_remote_url("https://gitlab.com/org/repo.git") == "gitlab"
    assert detect_provider_from_remote_url("ssh://git@example.com/org/repo.git") == "unknown"


def test_resolve_provider_uses_override_then_auto() -> None:
    assert resolve_provider(settings_provider="github", remote_url="https://x") == "github"
    assert resolve_provider(settings_provider="gitlab", remote_url="https://x") == "gitlab"
    assert (
        resolve_provider(
            settings_provider="none",
            remote_url="https://github.com/a/b",
        )
        == "unknown"
    )
    assert (
        resolve_provider(
            settings_provider="auto",
            remote_url="git@github.com:org/repo.git",
        )
        == "github"
    )


def test_get_remote_url_and_push_helpers(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        if cmd[-3:] == ["remote", "get-url", "origin"]:
            return _cp(stdout="git@github.com:org/repo.git\n")
        if cmd[-3:] == ["push", "origin", "feat/t01-a"]:
            return _cp(stdout="pushed branch\n")
        if cmd[-3:] == ["push", "origin", "main"]:
            return _cp(stdout="pushed base\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    remote_url = get_remote_url(repo, "origin")
    ok_branch, out_branch = push_branch(repo, remote="origin", branch="feat/t01-a")
    ok_base, out_base = push_base(repo, remote="origin", base_branch="main")

    assert remote_url == "git@github.com:org/repo.git"
    assert ok_branch is True and "pushed branch" in out_branch
    assert ok_base is True and "pushed base" in out_base
    assert any(cmd[-3:] == ["remote", "get-url", "origin"] for cmd in calls)


def test_create_pr_or_mr_uses_gh_for_github(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("alysis_code.remote_sync.shutil.which", lambda cmd: "/usr/bin/gh")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        assert cmd[:3] == ["gh", "pr", "create"]
        return _cp(stdout="https://github.com/org/repo/pull/12\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, url, output = create_pr_or_mr(
        repo,
        provider="github",
        base_branch="main",
        head_branch="feat/t01-a",
        title="T01: Task",
        body="Auto PR",
    )
    assert ok is True
    assert url == "https://github.com/org/repo/pull/12"
    assert "pull/12" in output
    assert calls


def test_create_pr_or_mr_uses_glab_for_gitlab(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("alysis_code.remote_sync.shutil.which", lambda cmd: "/usr/bin/glab")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        assert cmd[:3] == ["glab", "mr", "create"]
        return _cp(stdout="https://gitlab.com/org/repo/-/merge_requests/7\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, url, output = create_pr_or_mr(
        repo,
        provider="gitlab",
        base_branch="main",
        head_branch="feat/t01-a",
        title="T01: Task",
        body="Auto MR",
    )
    assert ok is True
    assert url == "https://gitlab.com/org/repo/-/merge_requests/7"
    assert "merge_requests/7" in output
    assert calls


def test_create_pr_or_mr_fails_when_cli_missing(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("alysis_code.remote_sync.shutil.which", lambda _cmd: None)
    ok, url, output = create_pr_or_mr(
        repo,
        provider="github",
        base_branch="main",
        head_branch="feat/t01-a",
        title="T01: Task",
        body="Auto PR",
    )
    assert ok is False
    assert url is None
    assert "not available" in output


def test_find_existing_pr_or_mr_for_github(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("alysis_code.remote_sync.shutil.which", lambda cmd: "/usr/bin/gh")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd[:3] == ["gh", "pr", "list"]
        return _cp(stdout='[{"number": 12, "url": "https://github.com/org/repo/pull/12"}]\n')

    monkeypatch.setattr(subprocess, "run", fake_run)
    found, url, pr_id, raw = find_existing_pr_or_mr(
        repo,
        provider="github",
        base_branch="main",
        head_branch="feat/t01-a",
    )
    assert found is True
    assert url == "https://github.com/org/repo/pull/12"
    assert pr_id == "12"
    assert "pull/12" in raw


def test_ensure_pr_or_mr_reuses_existing_when_create_fails(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = {"find": 0}

    def fake_find(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        calls["find"] += 1
        if calls["find"] == 1:
            return False, None, None, "none"
        return True, "https://github.com/org/repo/pull/99", "99", "existing"

    monkeypatch.setattr("alysis_code.remote_sync.find_existing_pr_or_mr", fake_find)
    monkeypatch.setattr(
        "alysis_code.remote_sync.create_pr_or_mr",
        lambda *_args, **_kwargs: (False, None, "already exists"),
    )

    ok, url, pr_id, output = ensure_pr_or_mr(
        repo,
        provider="github",
        base_branch="main",
        head_branch="feat/t01-a",
        title="T01: Task",
        body="Auto PR",
    )
    assert ok is True
    assert url == "https://github.com/org/repo/pull/99"
    assert pr_id == "99"
    assert "reused existing PR/MR" in output


def test_ensure_pr_or_mr_creates_when_missing(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "alysis_code.remote_sync.find_existing_pr_or_mr",
        lambda *_args, **_kwargs: (False, None, None, "none"),
    )
    monkeypatch.setattr(
        "alysis_code.remote_sync.create_pr_or_mr",
        lambda *_args, **_kwargs: (
            True,
            "https://gitlab.com/org/repo/-/merge_requests/7",
            "created",
        ),
    )

    ok, url, pr_id, output = ensure_pr_or_mr(
        repo,
        provider="gitlab",
        base_branch="main",
        head_branch="feat/t01-a",
        title="T01: Task",
        body="Auto MR",
    )
    assert ok is True
    assert url == "https://gitlab.com/org/repo/-/merge_requests/7"
    assert pr_id == "7"
    assert output == "created"
