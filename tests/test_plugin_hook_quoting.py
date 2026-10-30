"""Plugin hook commands preserve literal argv through the POSIX shell.

The shell probes only parse arguments with `set` and emit them with builtin
printf. No hook, git, provider or path named by a fixture is executed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code.cli import app
from alysis_code.extensions import install as install_mod


def _shell_argv(command: str, tmp_path: Path) -> list[str]:
    result = subprocess.run(
        ["/bin/sh", "-c", "set -- " + command + '; printf "%s\\0" "$@"'],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "P4_VALUE": "expanded-fixture"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return result.stdout.decode().rstrip("\0").split("\0")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell parser fixture")
@pytest.mark.parametrize("suffix", [".py", ".sh"])
@pytest.mark.parametrize(
    "directory",
    [
        "plain",
        "with space",
        "single'quote",
        'double"quote',
        "$P4_VALUE",
        "$(printf substituted)",
        "`printf substituted`",
        "semi;printf harmless",
        "star*question?",
        "line\nbreak",
    ],
)
def test_hook_path_stays_one_literal_argument(tmp_path, monkeypatch, directory, suffix):
    path = tmp_path / directory / ("start" + suffix)
    executable = "/fixture/python $P4_VALUE with space"
    monkeypatch.setattr(install_mod.sys, "executable", executable)
    expected = [executable, str(path)] if suffix == ".py" else [str(path)]
    assert _shell_argv(install_mod._hook_command(path), tmp_path) == expected


def test_windows_command_representation_is_retained(monkeypatch):
    path = Path("C:/Fixture Directory/start.py")
    with monkeypatch.context() as patch:
        patch.setattr(install_mod.sys, "platform", "win32")
        patch.setattr(install_mod.sys, "executable", "C:/Python Directory/python.exe")
        command = install_mod._hook_command(path)
    assert command == '"C:/Python Directory/python.exe" "C:/Fixture Directory/start.py"'


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell parser fixture")
def test_cli_install_persists_literal_hook_argv(tmp_path, monkeypatch):
    config = tmp_path / "config '$P4_VALUE'"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(config))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_CI", raising=False)

    def clone(*, git_url, commit, destination):
        (destination / "alysis-plugin.toml").write_text("""schema_version = 1
[plugin]
id = "fixture.quoting"
name = "Quoting fixture"
version = "1.0.0"
description = "Inert fixture"
author = "Fixture"
license = "MIT"
[compatibility]
alysis = ">=0.1,<1"
platforms = ["darwin", "linux", "windows"]
[[components.hook]]
event = "SessionStart"
path = "start.py"
id = "start"
""")
        (destination / "start.py").write_text("# never executed\n")

    monkeypatch.setattr(install_mod, "_clone_pinned_git_repo", clone)
    result = CliRunner().invoke(
        app,
        [
            "ext",
            "install",
            "git+https://example.invalid/plugin.git@" + "a" * 40,
            "--path",
            str(tmp_path),
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    hooks = json.loads((config / "hooks.json").read_text())
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    argv = _shell_argv(command, tmp_path)
    assert argv == [
        install_mod.sys.executable,
        str(config / "hooks/plugins/fixture-quoting/start.py"),
    ]
    assert Path(argv[1]).read_text() == "# never executed\n"
