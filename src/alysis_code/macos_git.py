"""Recognize literal POSIX Git invocations without granting a shell metadata writes.

Git subcommands, hooks and aliases run as real processes under Seatbelt. This
recognizer does not emulate Git or decide whether an operation is approved; it
separates literal Git commands/lists from shell syntax requiring the ordinary policy.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .config import ConfigError

GIT_EXECUTABLE = "/usr/bin/git"

# The entries of a Git directory that decide which programs a later Git runs
# or where it sends a push, for whoever runs it and wherever: configuration,
# hooks, the pointer to a shared directory (whose configuration and hooks are
# then the ones used), a rebase in progress, whose list of steps can hold
# commands to execute, and the two folders where Git still reads remote
# definitions written before remotes were kept in the configuration. A Git
# command that was not approved by a person never receives write access to
# them; see ``seatbelt_profile``. Everything else in the directory (objects,
# refs, the index, logs) is what staging, committing and branching write.
GIT_PROGRAM_ENTRIES = (
    "config",
    "config.worktree",
    "hooks",
    "commondir",
    "rebase-merge",
    "remotes",
    "branches",
)


def stage_global_preferences(home: Path) -> None:
    """Project identity/format/signing preferences into the private scratch HOME.

    Do not grant processes access to the real HOME, include files, credential
    helpers, aliases or hook commands. Preserve signing requirements: unavailable
    keys must fail a commit instead of silently producing an unsigned commit.
    """
    environment = {"HOME": str(Path.home()), "GIT_CONFIG_NOSYSTEM": "1"}
    for key in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL"):
        if key in os.environ:
            environment[key] = os.environ[key]
    pattern = (
        r"^(user\.(name|email|signingkey)|core\.(autocrlf|eol|safecrlf)|"
        r"(commit|tag)\.gpgsign|gpg\.format|i18n\.commitencoding)$"
    )
    try:
        result = subprocess.run(
            [
                GIT_EXECUTABLE,
                "--no-pager",
                "config",
                "--global",
                "--no-includes",
                "--null",
                "--get-regexp",
                pattern,
            ],
            cwd=home,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(
            "Could not read global Git preferences; no command was launched."
        ) from exc
    if result.returncode not in (0, 1):
        raise ConfigError("Invalid global Git preferences; no command was launched.")
    lines = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        key, separator, value = record.partition(b"\n")
        if not separator or len(value) > 4096:
            raise ConfigError("Invalid global Git preference.")
        section, name = key.rsplit(b".", 1)
        value = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
        value = value.replace(b"\n", b"\\n").replace(b"\t", b"\\t").replace(b"\b", b"\\b")
        lines.append(b"[" + section + b"]\n" + name + b' = "' + value + b'"\n')
    (home / ".gitconfig").write_bytes(b"".join(lines))


def literal_git_argv(command: str) -> list[str] | None:
    commands = _literal_git_commands(command)
    if commands is None or len(commands) != 1:
        return None
    return commands[0][0]


def git_metadata_writes(command: str) -> bool:
    """Whether a literal Git command is handed writable repository metadata.

    A literal Git command list that only inspects the repository (status, diff,
    log and the like) runs as any other sandboxed command does, with ``.git``
    write-protected. Every other literal Git command needs the metadata to do
    its work and receives it, except ``GIT_PROGRAM_ENTRIES``: whatever its
    options, aliases, hooks or the repository's own configuration make it run,
    it cannot leave configuration or hooks behind for a later Git outside the
    sandbox. Only a person's approval of ``permissions.git_metadata`` lifts that.
    """
    from .read_only_commands import is_read_only_command

    return literal_git_process(command) is not None and not is_read_only_command(
        command, dialect="posix"
    )


def git_creates_repository(command: str) -> bool:
    """Whether a literal command is a plain ``git init`` and nothing else.

    Creating a repository writes its first configuration and sample hooks, so
    it cannot run under the protection of ``GIT_PROGRAM_ENTRIES``. Without a
    template, a configuration option or a separate Git directory, Git chooses
    every byte of both itself and starts no other program, so nothing in the
    command decides what a later Git runs.
    """
    argv = literal_git_argv(command)
    if argv is None:
        return False
    words = argv[1:]
    if len(words) >= 2 and words[0] == "-C":
        words = words[2:]
    if words[:1] != ["init"]:
        return False
    directories = 0
    index = 1
    while index < len(words):
        word = words[index]
        if word in ("-b", "--initial-branch") and index + 1 < len(words):
            index += 1
        elif word.startswith("-") and word not in ("-q", "--quiet"):
            if not word.startswith("--initial-branch="):
                return False
        elif not word.startswith("-"):
            directories += 1
        index += 1
    return directories <= 1


def literal_git_process(command: str) -> list[str] | None:
    """Run literal Git command lists with POSIX exit/short-circuit semantics.

    Every executable is pinned Git. Quoted arguments are reconstructed, never
    evaluated as shell code. A mixed command or expansion uses the ordinary
    sandbox instead; it cannot acquire Git's metadata permission.
    """
    commands = _literal_git_commands(command)
    if commands is None:
        return None
    if len(commands) == 1:
        return commands[0][0]
    script = " ".join(
        shlex.join(argv) + (" " + operator if operator else "") for argv, operator in commands
    )
    return ["/bin/sh", "-c", script]


def _literal_git_commands(command: str) -> list[tuple[list[str], str]] | None:
    quote = None
    argv = []
    commands = []
    word = []
    started = False
    index = 0
    while index < len(command):
        character = command[index]
        index += 1
        if character == "\0":
            return None
        if quote == "'":
            if character == "'":
                quote = None
            else:
                word.append(character)
            continue
        if character == "\\":
            if index == len(command) or command[index] in "\n\0":
                return None
            following = command[index]
            index += 1
            if quote == '"' and following not in '\\$`"':
                word.append("\\")
            word.append(following)
            started = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            elif character in "$`":
                return None
            else:
                word.append(character)
            continue
        if character in "'\"":
            quote = character
            started = True
        elif character in ";\n&|":
            operator = ";" if character == "\n" else character
            if character in "&|":
                if index == len(command) or command[index] != character:
                    return None
                operator += character
                index += 1
            if started:
                argv.append("".join(word))
                word, started = [], False
            if not argv or argv[0] not in {"git", GIT_EXECUTABLE}:
                return None
            commands.append(([GIT_EXECUTABLE, *argv[1:]], operator))
            argv = []
        elif character == "~" and started and "".join(word)[-1:] not in ("=", ":"):
            # A tilde is only rewritten at the start of a word, or after = and :
            # in an assignment. Inside a word it is Git's own syntax (HEAD~1).
            word.append(character)
        elif character == "{" and word:
            # Braces inside a word that hold one plain item (stash@{0}, @{u},
            # HEAD@{1.day.ago}) are not a list or a range, so no shell expands them.
            end = command.find("}", index)
            inner = command[index:end] if end != -1 else None
            if (
                inner is None
                or ".." in inner
                or not all(c.isalnum() or c in "._:/+-" for c in inner)
            ):
                return None
            word.extend("{" + inner + "}")
            index = end + 1
        elif character in "\r<>()$`*?[]{}~#":
            return None
        elif character in " \t":
            if started:
                argv.append("".join(word))
                word = []
                started = False
        else:
            word.append(character)
            started = True
    if quote:
        return None
    if started:
        argv.append("".join(word))
    if not argv:
        return None
    if argv[0] not in {"git", GIT_EXECUTABLE}:
        return None
    # Never resolve an executable from an untrusted workspace/PATH entry.
    commands.append(([GIT_EXECUTABLE, *argv[1:]], ""))
    return commands
