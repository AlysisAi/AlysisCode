"""Shell commands that only look: the set no permission mode needs to ask about.

"Ask for approval" (``review``) runs these without asking and "Approve for me" (``auto``)
runs them without a review, the way Claude Code auto-allows read-only Bash commands and the
way Codex treated its "known safe" commands. Everything else keeps the mode's normal gate.

A command counts as read-only only when *every* part of it is provably read-only:

* it lexes cleanly (``shell_command_lexer``, strict): no ``$(…)``, backticks, script blocks,
  here-docs or grouping, whose contents this check would not see;
* every simple command in it (``a && b | c``) is on the list below, called by its bare name
  (``ls``, not ``./ls`` or ``C:\\tools\\ls.exe``), without an ``X=1`` environment prefix,
  and with arguments that keep it read-only (``find`` without ``-exec``/``-delete``,
  ``git branch`` without ``-D``, ``sed -n 5,20p`` but no other ``sed``);
* output goes to the terminal or a null device only (``2>&1``, ``>/dev/null``, ``2>nul``);
* no argument names a credential file (``.env``, ``~/.ssh/…``), which needs its own approval.

Shell wrappers (``cmd /c …``, ``powershell -Command …``, ``bash -c …``) are judged by what they
run. The list is deliberately short: a missing command only means the mode's usual gate
applies, while a wrong entry would let a write run unasked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from .shell_command_lexer import (
    Redirect,
    ShellDialect,
    SimpleCommand,
    host_dialect,
    split_simple_commands,
    unwrap_shell_payload,
)
from .tools.fs import classify_sensitive_path

_MAX_DEPTH = 3
_NULL_SINKS = {"/dev/null", "nul", "nul:", "$null"}

# Read-only with any arguments (the universal checks still apply).
_ANY_ARGUMENTS = frozenset(
    {
        # POSIX tools
        "basename",
        "cat",
        "cksum",
        "cmp",
        "column",
        "comm",
        "cut",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "egrep",
        "expand",
        "false",
        "fgrep",
        "fold",
        "grep",
        "groups",
        "head",
        "id",
        "ls",
        "md5sum",
        "nl",
        "nproc",
        "od",
        "pgrep",
        "printenv",
        "printf",
        "ps",
        "pwd",
        "readlink",
        "realpath",
        "rev",
        "sha1sum",
        "sha224sum",
        "sha256sum",
        "sha384sum",
        "sha512sum",
        "shasum",
        "stat",
        "strings",
        "tac",
        "tail",
        "test",
        "tr",
        "tree",
        "true",
        "type",
        "uname",
        "unexpand",
        "uptime",
        "wc",
        "whereis",
        "which",
        "whoami",
        "[",
        # cmd.exe built-ins and Windows tools
        "cd",
        "chdir",
        "dir",
        "findstr",
        "systeminfo",
        "tasklist",
        "ver",
        "vol",
        "where",
        # PowerShell cmdlets and their default aliases
        "compare-object",
        "convertfrom-json",
        "convertto-json",
        "format-list",
        "fl",
        "format-table",
        "ft",
        "format-wide",
        "fw",
        "gal",
        "gc",
        "gci",
        "gcm",
        "gdr",
        "get-alias",
        "get-childitem",
        "get-command",
        "get-computerinfo",
        "get-content",
        "get-culture",
        "get-date",
        "get-filehash",
        "get-help",
        "get-host",
        "get-item",
        "get-itemproperty",
        "get-location",
        "get-member",
        "get-module",
        "get-process",
        "get-psdrive",
        "get-service",
        "get-unique",
        "gi",
        "gl",
        "gp",
        "gps",
        "group-object",
        "gsv",
        "join-path",
        "measure",
        "measure-object",
        "out-string",
        "resolve-path",
        "rvpa",
        "select",
        "select-object",
        "select-string",
        "sls",
        "sort-object",
        "split-path",
        "test-path",
        "where-object",
        "write-host",
        "write-output",
    }
)

# Tools whose version or environment report is read-only: ``node --version``, ``go version``.
_VERSION_PROBE_TOOLS = frozenset(
    {
        "bun",
        "cargo",
        "clang",
        "clang++",
        "cmake",
        "conda",
        "deno",
        "docker",
        "dotnet",
        "g++",
        "gcc",
        "gem",
        "git",
        "go",
        "gradle",
        "java",
        "javac",
        "kubectl",
        "make",
        "mvn",
        "node",
        "npm",
        "npx",
        "perl",
        "php",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "pwsh",
        "py",
        "python",
        "python3",
        "ruby",
        "rustc",
        "rustup",
        "terraform",
        "tsc",
        "uv",
        "yarn",
    }
)
_VERSION_PROBE_ARGUMENTS = frozenset(
    {"--version", "-version", "-v", "-V", "--info", "--list-sdks", "--list-runtimes"}
)
# Tools whose bare ``version`` subcommand only prints (elsewhere it can run a build target,
# a script named "version", or bump a package version).
_VERSION_SUBCOMMAND_TOOLS = frozenset({"cargo", "docker", "go", "kubectl", "terraform"})

_FIND_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
_SED_PRINT_SCRIPT = re.compile(r"^(?:\d+|\$)(?:,(?:\d+|\$))?p$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_GIT_ANY_ARGUMENTS = frozenset(
    {
        "annotate",
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "count-objects",
        "describe",
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "var",
        "whatchanged",
    }
)
# Options that make an otherwise read-only git command write a file or run a program.
_GIT_WRITING_OPTION_PREFIXES = ("--output", "--open-files-in-pager", "-O")
_GIT_BRANCH_CHANGES = frozenset(
    {
        "-d",
        "-D",
        "--delete",
        "-m",
        "-M",
        "--move",
        "-c",
        "-C",
        "--copy",
        "-f",
        "--force",
        "-u",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
        "-t",
        "--track",
        "--no-track",
        "--create-reflog",
    }
)
_GIT_LIST_FILTERS = frozenset(
    {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
)
_GIT_TAG_CHANGES = frozenset(
    {
        "-a",
        "-s",
        "-u",
        "-f",
        "--force",
        "-d",
        "--delete",
        "-m",
        "-F",
        "-e",
        "--edit",
        "--annotate",
        "--sign",
        "--local-user",
        "--message",
        "--file",
        "--create-reflog",
    }
)
_GIT_CONFIG_READS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
        "--get-color",
        "--get-colorbool",
    }
)
_GIT_CONFIG_WRITES = frozenset(
    {
        "--unset",
        "--unset-all",
        "--add",
        "--replace-all",
        "--rename-section",
        "--remove-section",
        "-e",
        "--edit",
    }
)
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
_GIT_GLOBAL_FLAGS = frozenset(
    {
        "--no-pager",
        "-P",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
        "--no-optional-locks",
        "--exec-path",
        "--html-path",
        "--man-path",
        "--info-path",
    }
)


def is_read_only_command(command: str, *, dialect: ShellDialect | None = None) -> bool:
    """True when every part of ``command`` only reads (see the module docstring)."""

    return _command_line_is_read_only(str(command or ""), dialect or host_dialect(), depth=0)


def _command_line_is_read_only(command: str, dialect: ShellDialect, *, depth: int) -> bool:
    if depth > _MAX_DEPTH or not command.strip():
        return False
    commands = split_simple_commands(command, dialect=dialect, strict=True)
    if not commands:
        return False
    return all(_simple_command_is_read_only(item, dialect, depth=depth) for item in commands)


def _simple_command_is_read_only(item: SimpleCommand, dialect: ShellDialect, *, depth: int) -> bool:
    words = list(item.words)
    if not words or not all(_redirect_is_harmless(r) for r in item.redirects):
        return False
    if _ENV_ASSIGNMENT.match(words[0]):
        return False
    unwrapped = unwrap_shell_payload(words)
    if unwrapped is False:
        return False
    if unwrapped is not None:
        payload, payload_dialect = unwrapped
        return _command_line_is_read_only(payload, payload_dialect, depth=depth + 1)
    if any(names_credential_file(word) for word in words[1:]):
        return False
    name = _bare_command_name(words[0], dialect)
    if name is None:
        return False
    return _arguments_keep_it_read_only(name, words[1:], dialect)


def _bare_command_name(word: str, dialect: ShellDialect) -> str | None:
    """The command's name if it is called by bare name; ``None`` for a path or a variable."""

    if not word or any(sep in word for sep in "/\\") or word.startswith(("$", "%", "-", ".")):
        return None
    name = word.lower()
    if dialect != "posix":
        for suffix in (".exe", ".com"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
    return name


def _redirect_is_harmless(redirect: Redirect) -> bool:
    if redirect.target.startswith("&"):
        return redirect.target in {"&1", "&2"}
    if redirect.operator == "<":
        return not names_credential_file(redirect.target)
    return redirect.target.lower() in _NULL_SINKS


def names_credential_file(word: str) -> bool:
    candidate = word.split("=", 1)[1] if word.startswith("--") and "=" in word else word
    if not candidate or candidate.startswith("-") and len(candidate) < 3:
        return False
    looks_like_path = (
        any(sep in candidate for sep in "/\\")
        or candidate.startswith((".", "~"))
        or "." in candidate
    )
    return looks_like_path and classify_sensitive_path(candidate.lstrip("~")).sensitive


def _positionals(arguments: Sequence[str]) -> list[str]:
    return [a for a in arguments if not a.startswith("-")]


def _arguments_keep_it_read_only(name: str, arguments: list[str], dialect: ShellDialect) -> bool:
    special = _SPECIAL_RULES.get(name)
    if special is not None:
        return special(arguments, dialect)
    if name in _ANY_ARGUMENTS:
        return True
    if name in _VERSION_PROBE_TOOLS:
        return _is_version_probe(name, arguments)
    return False


def _is_version_probe(name: str, arguments: list[str]) -> bool:
    if len(arguments) != 1:
        return False
    return arguments[0] in _VERSION_PROBE_ARGUMENTS or (
        arguments[0] == "version" and name in _VERSION_SUBCOMMAND_TOOLS
    )


def _find(arguments: list[str], dialect: ShellDialect) -> bool:
    return not any(a.lower() in _FIND_ACTIONS for a in arguments)


def _rg(arguments: list[str], dialect: ShellDialect) -> bool:
    for argument in arguments:
        if argument.startswith(("--pre", "--search-zip", "--hostname-bin")):
            return False
        if re.fullmatch(r"-[A-Za-z]*z[A-Za-z]*", argument):
            return False
    return True


def _sed(arguments: list[str], dialect: ShellDialect) -> bool:
    quiet = {"-n", "--quiet", "--silent"}
    if not any(a in quiet for a in arguments):
        return False
    scripts = [a for a in arguments if a not in quiet and not a.startswith("-")]
    flags = [a for a in arguments if a.startswith("-") and a not in quiet]
    return not flags and bool(scripts) and bool(_SED_PRINT_SCRIPT.match(scripts[0]))


def _sort(arguments: list[str], dialect: ShellDialect) -> bool:
    for argument in arguments:
        lowered = argument.lower()
        if lowered.startswith("--output") or lowered == "/o":
            return False
        if re.fullmatch(r"-[A-Za-z]*o[A-Za-z]*", argument):
            return False
    return True


def _uniq(arguments: list[str], dialect: ShellDialect) -> bool:
    return len(_positionals(arguments)) <= 1  # a second operand is an output file


def _date(arguments: list[str], dialect: ShellDialect) -> bool:
    if dialect != "posix":
        # cmd's bare ``date`` prompts for a new system date; ``date /t`` only prints it.
        return [a.lower() for a in arguments] == ["/t"]
    return not any(
        a == "-s" or a.startswith("--set") or not a.startswith(("-", "+")) for a in arguments
    )


def _file(arguments: list[str], dialect: ShellDialect) -> bool:
    return not any(a in {"-C", "--compile"} for a in arguments)  # -C writes a magic file


def _fc(arguments: list[str], dialect: ShellDialect) -> bool:
    # cmd's file compare; in a POSIX shell ``fc`` edits and re-runs history.
    return dialect != "posix"


def _no_positionals(arguments: list[str], dialect: ShellDialect) -> bool:
    return not _positionals(arguments)


def _cmd_set(arguments: list[str], dialect: ShellDialect) -> bool:
    return len(arguments) <= 1 and not any("=" in a for a in arguments)


def _attrib(arguments: list[str], dialect: ShellDialect) -> bool:
    return not any(a.startswith(("+", "-")) for a in arguments)


def _first_argument_in(names: set[str]) -> Callable[[list[str], ShellDialect], bool]:
    def check(arguments: list[str], dialect: ShellDialect) -> bool:
        if len(arguments) == 1 and arguments[0] in _VERSION_PROBE_ARGUMENTS:
            return True
        return bool(arguments) and arguments[0].lower() in names

    return check


def _git(arguments: list[str], dialect: ShellDialect) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        option = argument.split("=", 1)[0]
        if option in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 1 if "=" in argument else 2
            continue
        if argument in _GIT_GLOBAL_FLAGS or argument in {"--version", "--help"}:
            index += 1
            continue
        break
    rest = arguments[index:]
    if not rest:
        return index > 0 and arguments[-1] in {"--version", "--exec-path"}
    subcommand, options = rest[0], rest[1:]
    if any(_git_option_writes(o) for o in options):
        return False
    if subcommand in _GIT_ANY_ARGUMENTS:
        return True
    if subcommand == "branch":
        if any(o.split("=", 1)[0] in _GIT_BRANCH_CHANGES for o in options):
            return False
        return not _positionals(options) or any(
            o.split("=", 1)[0] in _GIT_LIST_FILTERS for o in options
        )
    if subcommand == "tag":
        if any(o.split("=", 1)[0] in _GIT_TAG_CHANGES for o in options):
            return False
        return not _positionals(options) or any(o in {"-l", "--list"} for o in options)
    if subcommand == "remote":
        return (
            not options or options in (["-v"], ["--verbose"]) or (options[0] in {"show", "get-url"})
        )
    if subcommand in {"stash", "worktree", "notes"}:
        return bool(options) and options[0] in {"list", "show"}
    if subcommand == "reflog":
        return not options or options[0] == "show" or options[0].startswith("-")
    if subcommand == "config":
        if any(o.split("=", 1)[0] in _GIT_CONFIG_WRITES for o in options):
            return False
        if options and options[0] in {"list", "get"}:
            return True
        return any(o in _GIT_CONFIG_READS for o in options)
    return False


def _git_option_writes(option: str) -> bool:
    return option.startswith(_GIT_WRITING_OPTION_PREFIXES)


_SPECIAL_RULES: dict[str, Callable[[list[str], ShellDialect], bool]] = {
    "file": _file,
    "fc": _fc,
    "find": _find,
    "rg": _rg,
    "sed": _sed,
    "sort": _sort,
    "uniq": _uniq,
    "date": _date,
    "hostname": _no_positionals,
    "env": _no_positionals,
    "set": _cmd_set,
    "path": _no_positionals,
    "attrib": _attrib,
    "git": _git,
    "pip": _first_argument_in({"list", "show", "freeze"}),
    "pip3": _first_argument_in({"list", "show", "freeze"}),
    "npm": _first_argument_in({"ls", "list"}),
    "pnpm": _first_argument_in({"ls", "list"}),
}
