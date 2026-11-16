"""Guard: child-process pipes choose text mode only through alysis_code.command_text.

A subprocess call that sets ``text=True`` (or an encoding) itself decodes with whatever codec it
names or, without one, the Windows ANSI code page, and the first unexpected byte loses the whole
output. ``**command_text()`` applies the one rule documented in that module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alysis_code import consumer_verification

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "alysis_code"
SUBPROCESS_FUNCTIONS = {"run", "Popen", "check_output", "check_call", "call"}
TEXT_ARGUMENTS = {"text", "universal_newlines", "encoding", "errors"}
PROCESS_ARGUMENTS = {"stdout", "stderr", "stdin", "cwd", "env", "creationflags"}


def _text_mode_violations(tree: ast.AST, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_subprocess = (
                isinstance(func, ast.Attribute)
                and func.attr in SUBPROCESS_FUNCTIONS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            named = sorted(k.arg for k in node.keywords if k.arg in TEXT_ARGUMENTS)
            if is_subprocess and named:
                found.append(
                    f"{filename}:{node.lineno} subprocess.{func.attr}({', '.join(named)}=...)"
                )
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            named = sorted(keys & TEXT_ARGUMENTS)
            if named and keys & PROCESS_ARGUMENTS:
                found.append(
                    f"{filename}:{node.lineno} process arguments dict sets {', '.join(named)}"
                )
    return found


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


@pytest.fixture(scope="module")
def source_violations() -> tuple[list[str], list[str]]:
    # Keep both guards in this module: --dist loadfile runs this scan once.
    # Retain only diagnostics, not an AST cache of the entire source tree.
    text_mode: list[str] = []
    cmd_lists: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), relative)
        cmd_lists.extend(_list_form_cmd_invocations(tree, relative))
        if path != SOURCE_ROOT / "command_text.py":
            text_mode.extend(_text_mode_violations(tree, relative))
    # The consumer launcher is source code kept in a string; check it as well.
    cmd_lists.extend(
        _list_form_cmd_invocations(
            ast.parse(consumer_verification._LAUNCHER), "consumer_verification._LAUNCHER"
        )
    )
    return text_mode, cmd_lists


def test_subprocess_text_mode_goes_through_command_text(source_violations) -> None:
    text_mode, _ = source_violations
    assert not text_mode, (
        "Open child-process pipes in text mode with **command_text() "
        "(alysis_code/command_text.py), not text=/encoding=/errors=:\n" + "\n".join(text_mode)
    )


def test_no_code_hands_cmd_exe_a_list(source_violations) -> None:
    # Lists are quoted by subprocess using C-runtime rules, which cmd.exe does
    # not follow. Shell commands must reach it as one verbatim command line.
    _, cmd_lists = source_violations
    assert not cmd_lists, "cmd.exe list-form command lines:\n" + "\n".join(cmd_lists)


def test_the_guard_recognises_what_it_forbids() -> None:
    forbidden = [
        "subprocess.run(['git'], capture_output=True, text=True)",
        "subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)",
        "subprocess.check_output(cmd, encoding='utf-8')",
        "kwargs = {'stdout': subprocess.PIPE, 'text': True}",
    ]
    allowed = [
        "subprocess.run(['git'], capture_output=True, **command_text())",
        "subprocess.run(['git'], capture_output=True)",
        "Message(text=True)",
        "payload = {'type': 'text', 'text': value}",
    ]
    for snippet in forbidden:
        assert _text_mode_violations(ast.parse(snippet), "<snippet>"), snippet
    for snippet in allowed:
        assert not _text_mode_violations(ast.parse(snippet), "<snippet>"), snippet


def test_cmd_guard_recognises_list_and_tuple_switches() -> None:
    for snippet in ("['cmd.exe', '/c', command]", "('cmd.exe', '/K', command)"):
        assert _list_form_cmd_invocations(ast.parse(snippet), "<snippet>"), snippet
    for snippet in ('command = "cmd.exe /c echo hello"', "['python', '-c', command]"):
        assert not _list_form_cmd_invocations(ast.parse(snippet), "<snippet>"), snippet
