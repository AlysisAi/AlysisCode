"""Recursive deletes aimed at the project itself, a folder above it, or the home folder.

Full access runs commands without asking. Claude Code still asks before ``rm -rf`` on a
critical path in that mode, and so does this engine: a delete whose target *is* the
workspace root, a folder that contains it, the home folder (or a folder that contains it) or
a filesystem root is the kind of accident no model mistake should be able to cause
silently. ``build``, ``node_modules`` or ``src/old`` are not critical; ``.``, ``..``, ``*``
in the workspace root, ``~`` and ``$HOME`` are.

Detection parses the command (``shell_command_lexer``, lenient), finds recursive deletes
(``rm -r``, ``rd /s``, ``del /s``, ``Remove-Item -Recurse``, ``find … -delete``) through shell
wrappers, and resolves each target against the command's working folder. It is a seat belt
against accidents, not a sandbox: a determined script can delete the same files another way.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .shell_command_lexer import (
    ShellDialect,
    host_dialect,
    split_simple_commands,
    unwrap_shell_payload,
)
from .windows_command_policy import command_name

_MAX_DEPTH = 4
_REMOVE_ITEM = {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase"}
_HOME_SPELLINGS = re.compile(
    r"^(?:~|\$home|\$\{home\}|\$env:userprofile|%userprofile%|\$env:homedrive\$env:homepath"
    r"|%homedrive%%homepath%)(?=$|[\\/])",
    re.IGNORECASE,
)
_CWD_SPELLINGS = re.compile(r"^(?:\$pwd|\$\{pwd\}|%cd%|\$\(pwd\)|`pwd`)(?=$|[\\/])", re.IGNORECASE)
_TRAILING_WILDCARD = re.compile(r"(?:^|[\\/])\*(?:\.\*)?$")


def critical_delete_targets(
    command: str,
    *,
    cwd: Path,
    workspace_root: Path,
    home: Path | None = None,
    dialect: ShellDialect | None = None,
) -> list[Path]:
    """Critical folders ``command`` would delete recursively, resolved; empty when none."""

    home_path = _resolved(home if home is not None else Path.home())
    workspace = _resolved(workspace_root)
    protected = _protected_folders(workspace, home_path)
    found: list[Path] = []
    for target in _delete_targets(str(command or ""), dialect or host_dialect(), depth=0):
        resolved = _resolve_target(target, cwd=cwd, home=home_path)
        if resolved is not None and resolved in protected and resolved not in found:
            found.append(resolved)
    return found


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(path))


def _protected_folders(workspace: Path, home: Path) -> set[Path]:
    protected = {workspace, *workspace.parents, home, *home.parents}
    protected.add(Path(workspace.anchor))
    return protected


# Spellings of "here" and "home" that use shell grouping, which the lenient lexer splits.
_GROUPED_SPELLINGS = (
    ("$(pwd)", "$PWD"),
    ("`pwd`", "$PWD"),
    ("${PWD}", "$PWD"),
    ("${HOME}", "$HOME"),
)
# find expressions that only steer the walk; with nothing else, find … -delete empties it all.
_FIND_WALK_ONLY = {"-delete", "-depth", "-mindepth", "-maxdepth", "-xdev", "-mount"}


def _delete_targets(command: str, dialect: ShellDialect, *, depth: int) -> list[str]:
    if depth > _MAX_DEPTH:
        return []
    for spelling, plain in _GROUPED_SPELLINGS:
        command = command.replace(spelling, plain)
    targets: list[str] = []
    for item in split_simple_commands(command, dialect=dialect, strict=False) or []:
        words = list(item.words)
        while words and command_name(words[0]) in {
            "sudo",
            "doas",
            "command",
            "builtin",
            "call",
            "nohup",
        }:
            words = words[1:]
        if not words:
            continue
        unwrapped = unwrap_shell_payload(words)
        if unwrapped:
            payload, payload_dialect = unwrapped
            targets += _delete_targets(payload, payload_dialect, depth=depth + 1)
            continue
        targets += _recursive_delete_operands(words)
    return targets


def _recursive_delete_operands(words: list[str]) -> list[str]:
    name = command_name(words[0])
    arguments = words[1:]
    lowered = [a.lower() for a in arguments]
    if name == "rm" and any(
        _posix_flag(a, "r") or a in {"--recursive", "-recurse"} for a in arguments
    ):
        return [a for a in arguments if not a.startswith("-")]
    if name in {"rd", "rmdir"} and "/s" in lowered:
        return [a for a in arguments if not _is_cmd_switch(a)]
    if name in {"del", "erase"} and "/s" in lowered:
        return [a for a in arguments if not _is_cmd_switch(a)]
    if name in _REMOVE_ITEM and any(
        re.fullmatch(r"-r(?:e(?:c(?:u(?:r(?:se?)?)?)?)?)?", a) for a in lowered
    ):
        return _powershell_paths(arguments)
    if name in _REMOVE_ITEM and any(_TRAILING_WILDCARD.search(a) or a == "*" for a in arguments):
        # Remove-Item C:\project\* empties the folder even without -Recurse when it has no subfolders.
        return [a for a in _powershell_paths(arguments) if _TRAILING_WILDCARD.search(a) or a == "*"]
    if name == "find" and "-delete" in lowered:
        start = _first_expression(arguments)
        expression = arguments[start:]
        if all(a.lower() in _FIND_WALK_ONLY or a.isdigit() for a in expression):
            return arguments[:start] or ["."]
    return []


def _posix_flag(argument: str, flag: str) -> bool:
    return (
        argument.startswith("-")
        and not argument.startswith("--")
        and (flag in argument[1:] or flag.upper() in argument[1:])
    )


def _is_cmd_switch(argument: str) -> bool:
    return len(argument) >= 2 and argument[0] == "/" and argument[1] not in "\\/"


def _powershell_paths(arguments: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    for index, argument in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        lowered = argument.lower()
        if lowered in {"-path", "-literalpath", "-lp", "-pspath"}:
            if index + 1 < len(arguments):
                paths.append(arguments[index + 1])
            skip_next = True
            continue
        if lowered.startswith(("-path:", "-literalpath:")):
            paths.append(argument.split(":", 1)[1])
            continue
        if argument.startswith("-"):
            continue
        paths.extend(part for part in argument.split(",") if part)
    return paths


def _first_expression(arguments: list[str]) -> int:
    for index, argument in enumerate(arguments):
        if argument.startswith("-") or argument in {"(", "!"}:
            return index
    return len(arguments)


def _resolve_target(target: str, *, cwd: Path, home: Path) -> Path | None:
    text = target.strip().strip("\"'")
    if not text:
        return None
    text = _TRAILING_WILDCARD.sub("", text) or "."
    if any(ch in text for ch in "*?["):
        return None  # a pattern below a folder (src/*.tmp) never names the folder itself
    match = _HOME_SPELLINGS.match(text)
    if match:
        rest = text[match.end() :].lstrip("\\/")
        return _resolved(home / rest) if rest else home
    match = _CWD_SPELLINGS.match(text)
    if match:
        rest = text[match.end() :].lstrip("\\/")
        return _resolved(cwd / rest) if rest else _resolved(cwd)
    candidate = Path(text)
    if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:", text):
        candidate = cwd / candidate
    return _resolved(candidate)
