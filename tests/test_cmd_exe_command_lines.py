"""Commands reach cmd.exe as one verbatim command line, never as a list.

subprocess turns a list into a command line with backslash-escaped quotes (the C runtime's
rules). cmd.exe has its own rules and keeps those backslashes, so a quoted path such as
``"C:\\Program Files\\x.exe"`` arrives as ``\\"C:\\Program Files\\x.exe\\"`` and fails. Code that
runs a shell command string uses ``shell=True`` or builds the line itself
(``"<comspec>" /d /s /c "<command>"``). A list or tuple holding ``/c`` or ``/k`` is the bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

import alysis_code
from alysis_code import consumer_verification

SOURCE = Path(alysis_code.__file__).resolve().parent


def _list_form_cmd_invocations(tree: ast.AST, label: str) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for element in node.elts:
            if (
                isinstance(element, ast.Constant)
                and isinstance(element.value, str)
                and element.value.casefold() in {"/c", "/k"}
            ):
                found.append(f"{label}:{node.lineno}")
    return found


def test_no_code_hands_cmd_exe_a_list() -> None:
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += _list_form_cmd_invocations(tree, path.relative_to(SOURCE).as_posix())
    # The consumer launcher is source code kept in a string; check it the same way.
    launcher = ast.parse(consumer_verification._LAUNCHER)
    found += _list_form_cmd_invocations(launcher, "consumer_verification._LAUNCHER")
    assert found == []
