"""The engine's own Git on Windows computers without one (windows_toolchain)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code import windows_toolchain
from alysis_code.windows_toolchain import add_bundled_git_to_path


@pytest.mark.skipif(os.name != "nt", reason="Windows bundles its own verified Git")
def test_real_bundled_git_can_create_restore_points_without_system_git(tmp_path, monkeypatch):
    from alysis_code.ide.change_ledger import ChangeLedger

    no_git = tmp_path / "empty-path"
    no_git.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("original\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(no_git))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "engine-data"))
    commands = add_bundled_git_to_path()
    assert commands is not None and (commands / "git.exe").is_file()
    ledger = ChangeLedger(workspace)
    baseline = ledger.ensure_baseline("without-system-git")
    assert baseline.commit_oid
    assert ledger.head("without-system-git").checkpoint_id == baseline.checkpoint_id
    assert (workspace / "notes.txt").read_text() == "original\n"


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "windows-git"
    (root / "cmd").mkdir(parents=True)
    return root / "cmd" / "git.exe", root


def test_without_git_the_bundled_git_goes_last_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, root = _bundle(tmp_path)
    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {"git": (git, root)})
    monkeypatch.setattr(windows_toolchain.shutil, "which", lambda name, path=None: None)
    existing = str(tmp_path / "system-bin")
    env = {"PATH": existing}
    assert add_bundled_git_to_path(env, windows=True) == git.parent
    assert env["PATH"].split(os.pathsep) == [existing, str(git.parent)]


def test_an_installed_git_is_left_as_the_one_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, root = _bundle(tmp_path)
    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {"git": (git, root)})
    monkeypatch.setattr(
        windows_toolchain.shutil,
        "which",
        lambda name, path=None: r"C:\Program Files\Git\cmd\git.exe",
    )
    env = {"PATH": r"C:\Program Files\Git\cmd"}
    assert add_bundled_git_to_path(env, windows=True) is None
    assert env == {"PATH": r"C:\Program Files\Git\cmd"}


def test_an_empty_explicit_path_does_not_search_the_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, root = _bundle(tmp_path)
    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {"git": (git, root)})
    seen: list[str | None] = []

    def which(name, path=None):
        seen.append(path)
        return None if path == "" else "ambient-git"

    monkeypatch.setattr(windows_toolchain.shutil, "which", which)
    env: dict[str, str] = {}
    assert add_bundled_git_to_path(env, windows=True) == git.parent
    assert seen == [""]
    assert env["PATH"] == str(git.parent)


def test_other_systems_and_an_altered_bundle_change_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"PATH": "/usr/bin"}
    assert add_bundled_git_to_path(env, windows=False) is None
    assert env == {"PATH": "/usr/bin"}

    def altered() -> dict[str, tuple[Path, Path]]:
        raise ValueError("Bundled Git file integrity check failed: cmd/git.exe")

    monkeypatch.setattr(windows_toolchain, "installed_tools", altered)
    monkeypatch.setattr(windows_toolchain.shutil, "which", lambda name, path=None: None)
    env = {"PATH": r"C:\Windows"}
    assert add_bundled_git_to_path(env, windows=True) is None
    assert env == {"PATH": r"C:\Windows"}
