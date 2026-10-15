"""Split a shell command line into simple commands, the way the shell that runs it would.

The approval rules need to know what a command line *does*: which programs it starts, with
which arguments, and where their output goes. Regular expressions over the raw text get this
wrong in both directions (``2>&1`` is not a background ``&``; ``"a;b"`` is not two commands).
So this module lexes the line with the quoting rules of the shell that will run it:

* ``posix`` - ``/bin/sh`` (macOS, Linux): ``'…'`` is literal, ``"…"`` allows ``\\`` escapes,
  a bare ``\\`` escapes the next character.
* ``cmd`` - ``cmd.exe`` (the shell commands run in on Windows): only ``"…"`` quotes, ``'`` is
  an ordinary character, ``^`` escapes.
* ``powershell`` - the text after ``powershell -Command``: ``'…'`` and ``"…"`` quote.

Two strengths:

* ``strict=True`` answers "is this simple enough to judge?". Anything that can run code the
  lexer does not see (``$(…)``, backticks, ``<(…)``, here-docs, ``{…}`` script blocks,
  parentheses, cmd's ``^``, unbalanced quotes) makes it return ``None``. Callers treat
  ``None`` as "not provably safe".
* ``strict=False`` always returns something, for best-effort checks that must not give up on
  unusual syntax (the Full-access circuit breaker).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

ShellDialect = Literal["posix", "cmd", "powershell"]

_SEPARATORS = ("&&", "||", "|", ";", "&", "\r\n", "\n", "\r")
# Text that runs or expands code the lexer cannot see.
_OPAQUE_FRAGMENTS = ("`", "$(", "${", "<(", ">(", "<<", "@(", "@{", "@'", '@"', "--%")
_MAX_COMMAND_CHARS = 8_000


def host_dialect() -> ShellDialect:
    """The dialect of the shell that ``shell_run`` uses on this computer."""

    return "cmd" if os.name == "nt" else "posix"


@dataclass(frozen=True)
class Redirect:
    operator: str  # ">", ">>" or "<"
    stream: str  # "" (default), a digit, "*" (PowerShell all streams) or "&" (sh &>)
    target: str  # a word, or "&1"/"&2" for a stream duplication


@dataclass(frozen=True)
class SimpleCommand:
    words: tuple[str, ...]
    redirects: tuple[Redirect, ...] = ()
    separator_before: str = ""


@dataclass
class _State:
    words: list[str] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    chars: list[str] = field(default_factory=list)
    started: bool = False
    quoted: bool = False  # the current word contains a quoted part
    pending: tuple[str, str] | None = None  # redirect waiting for its target


class _Unsupported(Exception):
    pass


def split_simple_commands(
    command: str,
    *,
    dialect: ShellDialect,
    strict: bool = True,
) -> list[SimpleCommand] | None:
    text = str(command or "")
    if strict:
        if len(text) > _MAX_COMMAND_CHARS:
            return None
        if any(fragment in text for fragment in _OPAQUE_FRAGMENTS):
            return None
    try:
        return _lex(text, dialect=dialect, strict=strict)
    except _Unsupported:
        return None


def _lex(text: str, *, dialect: ShellDialect, strict: bool) -> list[SimpleCommand]:
    commands: list[SimpleCommand] = []
    state = _State()
    separator_before = ""
    quote = ""
    quote_chars = '"' if dialect == "cmd" else "'\""
    i = 0
    n = len(text)

    def end_word() -> None:
        if not state.started and not state.chars:
            return
        word = "".join(state.chars)
        state.chars.clear()
        state.started = False
        state.quoted = False
        if state.pending is not None:
            operator, stream = state.pending
            state.pending = None
            state.redirects.append(Redirect(operator, stream, word))
        else:
            state.words.append(word)

    def end_command(next_separator: str) -> None:
        nonlocal separator_before, state
        end_word()
        if state.pending is not None:
            if strict:
                raise _Unsupported("redirect without a target")
            state.pending = None
        if state.words or state.redirects:
            if strict and not state.words:
                raise _Unsupported("redirect without a command")
            commands.append(
                SimpleCommand(tuple(state.words), tuple(state.redirects), separator_before)
            )
        separator_before = next_separator
        state = _State()

    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                # PowerShell doubles a quote to embed it.
                if dialect == "powershell" and i + 1 < n and text[i + 1] == quote:
                    state.chars.append(ch)
                    i += 2
                    continue
                quote = ""
                i += 1
                continue
            if dialect == "posix" and quote == '"' and ch == "\\" and i + 1 < n:
                following = text[i + 1]
                if following in '"\\$`\n':
                    state.chars.append(following)
                    i += 2
                    continue
            state.chars.append(ch)
            i += 1
            continue

        if ch in quote_chars:
            quote = ch
            state.started = True
            state.quoted = True
            i += 1
            continue
        if dialect == "posix" and ch == "\\":
            if i + 1 < n:
                state.chars.append(text[i + 1])
                state.started = True
                i += 2
                continue
            i += 1
            continue
        if dialect == "cmd" and ch == "^":
            if strict:
                raise _Unsupported("cmd escape character")
            if i + 1 < n:
                state.chars.append(text[i + 1])
                state.started = True
            i += 2
            continue
        if ch in "(){}":
            if strict:
                raise _Unsupported("grouping or script block")
            end_command(";")
            i += 1
            continue
        if ch == "&" and i + 1 < n and text[i + 1] == ">" and dialect == "posix":
            end_word()
            operator, i = _redirect_operator(text, i + 1)
            i = _redirect_target(text, i, state, operator, "&")
            continue
        if ch in "<>":
            stream = ""
            word = "".join(state.chars)
            if (
                state.chars
                and not state.quoted
                and (word.isdigit() or (dialect == "powershell" and word == "*"))
            ):
                stream = word
                state.chars.clear()
                state.started = False
            else:
                end_word()
            operator, i = _redirect_operator(text, i)
            i = _redirect_target(text, i, state, operator, stream)
            continue
        separator = next((s for s in _SEPARATORS if text.startswith(s, i)), None)
        if separator is not None:
            normalized = "\n" if separator in {"\r\n", "\r"} else separator
            end_command(normalized)
            i += len(separator)
            continue
        if ch.isspace():
            end_word()
            i += 1
            continue
        state.chars.append(ch)
        state.started = True
        i += 1

    if quote and strict:
        raise _Unsupported("unbalanced quote")
    end_command("")
    return commands


def _redirect_operator(text: str, i: int) -> tuple[str, int]:
    ch = text[i]
    if ch == "<":
        return "<", i + 1
    if text.startswith(">>", i):
        return ">>", i + 2
    if text.startswith(">|", i):
        return ">", i + 2
    return ">", i + 1


def _redirect_target(text: str, i: int, state: _State, operator: str, stream: str) -> int:
    if i < len(text) and text[i] == "&":
        j = i + 1
        while j < len(text) and (text[j].isdigit() or text[j] == "-"):
            j += 1
        state.redirects.append(Redirect(operator, stream, text[i:j]))
        return j
    state.pending = (operator, stream)
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def unwrap_shell_payload(
    words: tuple[str, ...] | list[str],
) -> tuple[str, ShellDialect] | None | Literal[False]:
    """The command text a shell wrapper runs (``cmd /c …``, ``powershell -Command …``, ``sh -c …``).

    Returns ``None`` when ``words`` is not a shell wrapper, ``False`` when it is one whose
    payload cannot be read (an interactive shell, ``-File``, an undecodable
    ``-EncodedCommand``), and ``(payload, dialect)`` otherwise.
    """

    from .windows_command_policy import command_name, powershell_payload

    if not words:
        return None
    name = command_name(words[0])
    arguments = list(words[1:])
    if name == "cmd":
        for index, argument in enumerate(arguments):
            if argument.lower() in {"/c", "/k", "/r"}:
                return " ".join(arguments[index + 1 :]), "cmd"
        return False
    if name in {"powershell", "pwsh"}:
        payload, readable = powershell_payload(arguments)
        if not readable or not payload:
            return False
        return payload, "powershell"
    if name in {"sh", "bash", "dash", "zsh"}:
        for index, argument in enumerate(arguments):
            if (
                argument.startswith("-")
                and not argument.startswith("--")
                and argument.endswith("c")
            ):
                if index + 1 < len(arguments):
                    return arguments[index + 1], "posix"
                return False
            if not argument.startswith("-"):
                return False
        return False
    return None
