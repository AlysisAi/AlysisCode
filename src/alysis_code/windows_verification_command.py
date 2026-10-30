"""Recognize literal Windows verifier invocations without POSIX path escaping."""

from __future__ import annotations

import re

from .shell_command_lexer import split_simple_commands

_EXECUTABLE = re.compile(
    r"""^\s*(?:&\s*)?(?:"[^"\r\n]+\.(?:exe|cmd)"|'[^'\r\n]+\.(?:exe|cmd)'|[^\s'"&|;<>]+\.(?:exe|cmd))(?=\s|$)""",
    re.IGNORECASE,
)


def is_windows_verifier_command(command: str) -> bool:
    # Do not reinterpret ordinary POSIX commands or shell wrappers.
    return _EXECUTABLE.match(command) is not None


def split_windows_verifier_command(command: str) -> list[str] | None:
    """Accept one literal executable plus arguments; never evaluate shell syntax.

    PowerShell's initial call operator invokes a quoted executable. It is not
    a background operator. All other separators, redirects, substitutions and
    dynamic arguments remain unsupported, even with trusted command provenance.
    """
    text = command.strip()
    if any(char in text for char in ("\n", "\r", "$", "`", "#")):
        return None
    if text.startswith("&"):
        text = text[1:].lstrip()
    # The shared lexer records separators before commands, not trailing ones.
    if not text or text[-1] in "&|;<>":
        return None
    commands = split_simple_commands(text, dialect="powershell", strict=True)
    if not commands or len(commands) != 1:
        return None
    parsed = commands[0]
    if parsed.separator_before or parsed.redirects:
        return None
    return list(parsed.words)


def quote_windows_verifier_parts(parts: list[str]) -> str:
    return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in parts)
