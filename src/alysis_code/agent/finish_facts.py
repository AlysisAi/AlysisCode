"""Facts the host states to the model once before a turn ends.

The host does not read from the wording of a request what the request requires.
It does observe facts that are the same in every language: a command line the
user wrote was not run or last failed, a file the turn created does not parse
as its type, a path the user wrote is not there. The model is told these once
and decides, from its own reading of the request, whether any of them matters.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_ITEMS = 5
_MAX_PARSED_BYTES = 1_000_000


@dataclass(frozen=True)
class FinishFacts:
    commands_not_run: tuple[str, ...] = ()
    commands_failed: tuple[tuple[str, int | None], ...] = ()
    unparseable_files: tuple[tuple[str, str], ...] = ()
    missing_paths: tuple[str, ...] = ()
    services_not_ready: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.commands_not_run
            or self.commands_failed
            or self.unparseable_files
            or self.missing_paths
            or self.services_not_ready
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "commands_not_run": list(self.commands_not_run),
            "commands_failed": [list(item) for item in self.commands_failed],
            "unparseable_files": [list(item) for item in self.unparseable_files],
            "missing_paths": list(self.missing_paths),
            "services_not_ready": list(self.services_not_ready),
        }

    def message(self) -> str:
        """The model-facing statement of these facts; empty when there are none."""
        lines: list[str] = []
        if self.commands_not_run:
            lines.append(
                "- Written in the request as a command and not run in this turn: "
                + ", ".join(f"`{command}`" for command in self.commands_not_run)
                + "."
            )
        if self.commands_failed:
            lines.append(
                "- Written in the request as a command; its last run in this turn failed: "
                + ", ".join(
                    f"`{command}`" + (f" (exit code {code})" if code is not None else "")
                    for command, code in self.commands_failed
                )
                + "."
            )
        if self.unparseable_files:
            lines.append(
                "- Created in this turn and not valid for its file type: "
                + "; ".join(f"{path} ({reason})" for path, reason in self.unparseable_files)
                + "."
            )
        if self.missing_paths:
            lines.append(
                "- Written in the request as a path and not present: "
                + ", ".join(self.missing_paths)
                + "."
            )
        if self.services_not_ready:
            lines.append(
                "- Started in this turn as a durable service, not stopped by you, and not "
                "ready now: " + "; ".join(self.services_not_ready) + "."
            )
        if not lines:
            return ""
        return (
            "Facts the host observed in this turn, stated without judging the request. "
            "Re-read the request: act on a fact only if the request asks for it "
            "(for example run the command, repair the file, create the path, restart the "
            "service), and "
            "otherwise finish. This is stated once.\n" + "\n".join(lines)
        )


_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "{", "}"})
# One interpreter under several names is still the command the user wrote.
_SAME_PROGRAM = ({"python", "python3", "py"}, {"pip", "pip3"})


def _segments(command: str) -> list[list[str]]:
    """The simple commands of a command line, each as its words.

    Split on shell operators so that `cd app && pytest -q` and `(pytest -q; echo $?)`
    each contain `pytest -q` as one command. A quoted argument that is itself a
    command line (`bash -lc "pytest -q"`) is looked into once.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = [[]]
    nested: list[str] = []
    for token in tokens:
        if token in _OPERATORS or (token and set(token) <= set("&|;()")):
            segments.append([])
            continue
        segments[-1].append(token)
        if " " in token.strip():
            nested.append(token)
    for token in nested:
        try:
            inner = shlex.shlex(token, posix=True, punctuation_chars=True)
            inner.whitespace_split = True
            words: list[str] = []
            for word in inner:
                if word in _OPERATORS or (word and set(word) <= set("&|;()")):
                    segments.append(words)
                    words = []
                else:
                    words.append(word)
            segments.append(words)
        except ValueError:
            continue
    return [segment for segment in segments if segment]


def _same_program(written: str, observed: str) -> bool:
    if written == observed:
        return True
    if "/" in written or "\\" in written:
        return False
    # A program the user wrote by name may be run through its full path.
    name = PurePosixPath(observed.replace("\\", "/")).name
    return name == written or any(written in group and name in group for group in _SAME_PROGRAM)


def command_was_run_as(written: str, observed: str) -> bool:
    """Whether an observed command line ran the written one.

    The written words must appear next to each other, in order, inside one
    simple command of what ran: `pytest -q` is in `.venv/bin/pytest -q tests/`,
    in `cd app && pytest -q` and in `python -m pytest -q`, and is not in
    `pytest tests/` or in two unrelated commands that share its words.
    """
    try:
        wanted = shlex.split(written)
    except ValueError:
        wanted = written.split()
    while len(wanted) > 1 and "=" in wanted[0] and not wanted[0].startswith("="):
        wanted = wanted[1:]  # VAR=value prefixes are not the command
    if not wanted:
        return False
    for words in _segments(observed):
        for start in range(len(words) - len(wanted) + 1):
            if _same_program(wanted[0], words[start]) and all(
                wanted[offset] == words[start + offset] for offset in range(1, len(wanted))
            ):
                return True
    return False


def _without_comments(text: str) -> str:
    """JSON text with // and /* */ comments removed, strings left alone."""
    out: list[str] = []
    index, quoted = 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _parse_problem(path: Path) -> str | None:
    """Why a structured file does not parse, or None when it does or cannot be judged.

    Only formats with one strict grammar are judged. YAML is not: templates
    and custom tags are ordinary there. JSON with comments, as editors and
    compilers accept for their settings files, is not reported.
    """
    suffix = path.suffix.casefold()
    if suffix not in {".json", ".toml"}:
        return None
    try:
        if not path.is_file() or path.stat().st_size > _MAX_PARSED_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        if suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                json.loads(_without_comments(text))
                if "//" not in text and "/*" not in text:
                    raise
        else:
            import tomllib

            tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 - every parser failure is the fact reported
        label = "JSON" if suffix == ".json" else "TOML"
        return f"{label}: {' '.join(str(exc).split())[:120]}"
    return None


def collect_finish_facts(
    *,
    root: Path,
    request_commands: Iterable[str],
    required_commands: Iterable[str] = (),
    observed_commands: Iterable[tuple[str, bool | None, int | None]] = (),
    created_paths: Iterable[str] = (),
    written_paths: Iterable[str] = (),
    services_not_ready: Iterable[str] = (),
) -> FinishFacts:
    """Gather the facts for one turn.

    `request_commands` are command lines the user wrote; `required_commands`
    are those the model declared as requirements, which the completion gate
    already enforces. `observed_commands` are (command, passed, exit code) in
    the order they ran this turn. `created_paths` are files the turn created and
    `written_paths` are workspace paths the user wrote as paths and the model
    did not declare. `services_not_ready` describes durable services the turn
    started, did not stop itself, and that are not ready now.
    """
    observed = [item for item in observed_commands if item[0]]
    enforced = set(required_commands)
    not_run: list[str] = []
    failed: list[tuple[str, int | None]] = []
    for command in request_commands:
        if command in enforced:
            continue
        runs = [item for item in observed if command_was_run_as(command, item[0])]
        if not runs:
            not_run.append(command)
        elif runs[-1][1] is False:
            failed.append((command, runs[-1][2]))
    unparseable: list[tuple[str, str]] = []
    for relative in sorted(set(created_paths)):
        problem = _parse_problem(root / relative)
        if problem:
            unparseable.append((relative, problem))
    missing: list[str] = []
    for relative in written_paths:
        try:
            present = (root / relative).exists()
        except OSError:
            continue
        if not present and relative not in missing:
            missing.append(relative)
    return FinishFacts(
        commands_not_run=tuple(not_run[:_MAX_ITEMS]),
        commands_failed=tuple(failed[:_MAX_ITEMS]),
        unparseable_files=tuple(unparseable[:_MAX_ITEMS]),
        missing_paths=tuple(missing[:_MAX_ITEMS]),
        services_not_ready=tuple(list(services_not_ready)[:_MAX_ITEMS]),
    )
