"""Recursive deletes of the project, a folder above it, or the home folder are found."""

from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.critical_paths import critical_delete_targets


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home" / "me"
    project = home / "code" / "app"
    (project / "src").mkdir(parents=True)
    return {"home": home.resolve(), "project": project.resolve()}


def _targets(
    command: str, layout: dict[str, Path], *, dialect: str, cwd: Path | None = None
) -> list[Path]:
    return critical_delete_targets(
        command,
        cwd=cwd or layout["project"],
        workspace_root=layout["project"],
        home=layout["home"],
        dialect=dialect,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("dialect", "command", "expected"),
    [
        ("posix", "rm -rf .", "project"),
        ("posix", "rm -rf ./*", "project"),
        ("posix", "rm -r *", "project"),
        ("posix", "rm -rf ..", "code"),
        ("posix", "rm -rf src/..", "project"),
        ("posix", "rm -rf ~", "home"),
        ("posix", "rm -rf $HOME", "home"),
        ("posix", 'rm -rf "${HOME}"', "home"),
        ("posix", "rm -rf $(pwd)", "project"),
        ("posix", "sudo rm -Rf .", "project"),
        ("posix", "find . -delete", "project"),
        ("posix", "bash -c 'rm -rf ..'", "code"),
        ("cmd", "rd /s /q .", "project"),
        ("cmd", "rmdir /S /Q ..", "code"),
        ("cmd", "del /s /q *", "project"),
        ("cmd", "rd /s /q %USERPROFILE%", "home"),
        ("cmd", 'cmd /c "rd /s /q ."', "project"),
        ("cmd", 'powershell -Command "Remove-Item -Recurse -Force ."', "project"),
        ("powershell", "Remove-Item -Recurse -Force $env:USERPROFILE", "home"),
        ("powershell", "Remove-Item -Path .\\* -Recurse", "project"),
        ("powershell", "ri -r ..", "code"),
    ],
)
def test_critical_targets_are_found(layout, dialect, command, expected) -> None:  # type: ignore[no-untyped-def]
    paths = {
        "project": layout["project"],
        "code": layout["project"].parent,
        "home": layout["home"],
    }
    assert paths[expected] in _targets(command, layout, dialect=dialect)


@pytest.mark.parametrize(
    ("dialect", "command"),
    [
        ("posix", "rm -rf build"),
        ("posix", "rm -rf node_modules dist"),
        ("posix", "rm -f ."),
        ("posix", "rm -rf src/*.tmp"),
        ("posix", "find . -name '*.pyc' -delete"),
        ("posix", "echo rm -rf ."),
        ("posix", "rm -rf ../other-project"),
        ("cmd", "rd /s /q build"),
        ("cmd", "rd ."),
        ("powershell", "Remove-Item -Recurse -Force .\\dist"),
        ("powershell", "Get-ChildItem -Recurse ."),
    ],
)
def test_ordinary_deletes_are_not_critical(layout, dialect, command) -> None:  # type: ignore[no-untyped-def]
    assert _targets(command, layout, dialect=dialect) == []


def test_targets_resolve_against_the_working_folder(layout) -> None:  # type: ignore[no-untyped-def]
    src = layout["project"] / "src"
    assert _targets("rm -rf ..", layout, dialect="posix", cwd=src) == [layout["project"]]
    assert _targets("rm -rf .", layout, dialect="posix", cwd=src) == []
