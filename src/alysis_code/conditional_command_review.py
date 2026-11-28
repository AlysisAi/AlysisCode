"""Route literal filesystem inspections with conditionals to the command reviewer."""

from __future__ import annotations

import re
from collections import deque

from .read_only_commands import is_read_only_command, names_credential_file
from .shell_command_lexer import ShellDialect, split_simple_commands


def conditional_inspection_reviewable(command: str, dialect: ShellDialect) -> bool:
    """Validate the guard and every branch. The reviewer receives the original text."""
    if (
        dialect not in {"powershell", "posix"}
        or len(command) > 8000
        or not re.search(r"\bif[ \t(]", command, re.IGNORECASE)
    ):
        return False
    # Only literal words and file paths. No expansions, redirections, comments,
    # command substitutions, or quoting rules specific to another shell.
    if not command.isascii() or re.search(r"""[^\w \t\r\n./\\:*?,='"\[\](){};|-]""", command):
        return False
    if dialect == "posix" and "\\" in command:
        return False
    text = command.replace("\r\n", "\n")
    if "\r" in text:
        return False
    # A delimiter inside a quoted string can only split it into incomplete
    # fragments, which the strict lexer below rejects.
    parts: deque[str] = deque()
    for raw in re.split(r"([(){};\n])", text):
        token = raw.strip(" \t")
        if not token:
            continue
        if dialect == "posix" and (header := re.fullmatch(r"(then|else)[ \t]+(.+)", token)):
            parts.extend(header.groups())
        else:
            parts.append(
                token.lower()
                if dialect == "powershell" and token.lower() in {"if", "else"}
                else token
            )
    guarded = False

    def take(value: str) -> bool:
        if parts and parts[0] == value:
            parts.popleft()
            return True
        return False

    def separators() -> None:
        while parts and parts[0] in {";", "\n"}:
            parts.popleft()

    def guard(fragment: str) -> bool:
        parsed = split_simple_commands(fragment, dialect=dialect)
        if not parsed or len(parsed) != 1 or parsed[0].redirects:
            return False
        words = list(parsed[0].words)
        if dialect == "powershell":
            return (
                words[0].lower() == "test-path"
                and len(words) > 1
                and is_read_only_command(fragment, dialect=dialect)
            )
        if words[0] == "[" and words[-1] == "]":
            words = ["test", *words[1:-1]]
        return (
            len(words) == 3
            and words[0] == "test"
            and words[1] in {"-d", "-e", "-f"}
            and not names_credential_file(words[2])
        )

    def statements(depth: int, ends: set[str]) -> bool:
        nonlocal guarded
        if depth > 8:
            return False
        separators()
        seen = False
        while parts and parts[0] not in ends:
            part = parts.popleft()
            if dialect == "powershell" and part == "if":
                if not take("(") or not parts or not guard(parts.popleft()) or not take(")"):
                    return False
                separators()
                if not take("{") or not statements(depth + 1, {"}"}) or not take("}"):
                    return False
                guarded = True
                multiline = False
                while take("\n"):
                    multiline = True
                if take("else"):
                    while take("\n"):
                        pass
                    if not take("{") or not statements(depth + 1, {"}"}) or not take("}"):
                        return False
                elif multiline:
                    seen = True
                    continue
            elif dialect == "posix" and part.startswith(("if ", "if\t")):
                if not guard(part[2:].strip()):
                    return False
                separators()
                if not take("then") or not statements(depth + 1, {"else", "fi"}):
                    return False
                if take("else") and not statements(depth + 1, {"fi"}):
                    return False
                if not take("fi"):
                    return False
                guarded = True
            elif not is_read_only_command(part, dialect=dialect):
                return False
            seen = True
            if parts and parts[0] not in ends and parts[0] not in {";", "\n"}:
                return False
            separators()
        return seen

    return statements(0, set()) and not parts and guarded
