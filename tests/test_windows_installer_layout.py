"""The Windows installer is one script block that installs the package once.

These checks read the script, so they run on every platform; the installer's
behaviour tests in ``test_windows_installer.py`` need Windows and real uv, and
they only reach failure paths, which stop before anything after the block.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install.ps1"


def _statements() -> list[str]:
    return [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_nothing_runs_outside_the_installer_block():
    statements = _statements()
    assert statements[0] == "& {"
    assert statements[-1] == "}"
    # A statement after the block would run after a successful installation,
    # outside its error handling, version pin and Python choice.
    assert [line for line in statements[1:-1] if not line.startswith("    ")] == []


def test_the_package_is_installed_by_one_command():
    statements = _statements()
    installs = [line for line in statements if "'tool', 'install'" in line]
    assert len(installs) == 1
    # The one install honours ALYSIS_PYTHON and ALYSIS_VERSION.
    assert "$pythonVersion" in installs[0] and "$package" in installs[0]
    assert not [line for line in statements if "tool install" in line]
