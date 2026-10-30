"""Automatic update maintenance must not compete with unattended task startup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_cold_cli_registration_and_unattended_update_check_stay_local(tmp_path: Path):
    code = """
import sys
from alysis_code import cli
from alysis_code.cli_impl.commands import root
assert not sys.stdin.isatty() and not sys.stdout.isatty()
root._start_background_update_check()
for name in ('alysis_code.cli_impl.commands.update', 'alysis_code.updates', 'httpx', 'httpcore'):
    assert name not in sys.modules, name
# Selecting the update group still exposes the canonical command implementation.
from typer.main import get_group
from click import Context
group = get_group(cli.app)
command = group.get_command(Context(group), 'update')
assert command is not None
assert 'check' in command.list_commands(Context(command))
assert 'alysis_code.cli_impl.commands.update' in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
            ALYSIS_DATA_DIR=str(tmp_path / "data"),
        ),
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "stdin_tty,stdout_tty", [(False, False), (False, True), (True, False), (True, True)]
)
def test_automatic_update_refresh_requires_interactive_terminal(monkeypatch, stdin_tty, stdout_tty):
    from types import SimpleNamespace

    from alysis_code.cli_impl.commands import root, update

    calls = []
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: stdin_tty))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: stdout_tty))
    monkeypatch.setattr(update, "_start_background_update_check", lambda: calls.append(True))
    root._start_background_update_check()
    assert calls == ([True] if stdin_tty and stdout_tty else [])
