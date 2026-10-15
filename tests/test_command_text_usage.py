"""Guard: child-process pipes choose text mode only through alysis_code.command_text.

A subprocess call that sets ``text=True`` (or an encoding) itself decodes with whatever codec it
names or, without one, the Windows ANSI code page, and the first unexpected byte loses the whole
output. ``**command_text()`` applies the one rule documented in that module.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "alysis_code"
SUBPROCESS_FUNCTIONS = {"run", "Popen", "check_output", "check_call", "call"}
TEXT_ARGUMENTS = {"text", "universal_newlines", "encoding", "errors"}
PROCESS_ARGUMENTS = {"stdout", "stderr", "stdin", "cwd", "env", "creationflags"}


def _violations(source: str, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
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


def test_subprocess_text_mode_goes_through_command_text() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path.name == "command_text.py" and path.parent == SOURCE_ROOT:
            continue
        relative = path.relative_to(SOURCE_ROOT.parent).as_posix()
        violations += _violations(path.read_text(encoding="utf-8"), relative)
    assert not violations, (
        "Open child-process pipes in text mode with **command_text() "
        "(alysis_code/command_text.py), not text=/encoding=/errors=:\n" + "\n".join(violations)
    )


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
        assert _violations(snippet, "<snippet>"), snippet
    for snippet in allowed:
        assert not _violations(snippet, "<snippet>"), snippet
