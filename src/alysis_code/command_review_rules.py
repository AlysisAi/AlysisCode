"""Commands that run without a sandbox prompt but still deserve a decision in "Approve for me".

In ``auto`` mode a sandboxed command runs without asking. A few kinds of command can do
harm that a workspace sandbox does not contain, because the harm is to history, to shared
systems or to secrets rather than to files outside the project:

* git commands that throw work away or rewrite history (``reset --hard``, ``clean -f``,
  ``checkout -- .``, ``restore``, ``branch -D``, ``stash drop``, ``filter-branch``);
* publishing and deploying (``npm publish``, ``twine upload``, ``docker push``,
  ``gh release create``, ``terraform apply``, ``kubectl delete``, cloud CLIs that change
  resources);
* commands that name a credential file (``.env``, ``~/.ssh/id_rsa``, ``.aws/credentials``).

These get ``needs_confirm`` (``policy.evaluate_shell_command``): the reviewer decides in
"Approve for me", a person in "Ask for approval" (which asks for every command anyway), and
Full access normally runs them. The independent ``github_write_reason`` gate below
takes precedence for remote writes and unresolved effects: those always need a person.

Matching parses the command (``shell_command_lexer``) instead of searching its text, so
``echo "git reset --hard"`` is not a reset and ``cmd /c "git clean -fdx"`` is.
"""

from __future__ import annotations

import re

from .read_only_commands import names_credential_file
from .shell_command_lexer import (
    ShellDialect,
    host_dialect,
    split_simple_commands,
    unwrap_shell_payload,
)
from .windows_command_policy import command_name

_MAX_DEPTH = 4

_PUBLISH: dict[str, set[str]] = {
    "npm": {"publish", "unpublish", "deprecate"},
    "pnpm": {"publish"},
    "yarn": {"publish"},
    "bun": {"publish"},
    "cargo": {"publish", "yank"},
    "gem": {"push", "yank"},
    "twine": {"upload"},
    "docker": {"push"},
    "podman": {"push"},
    "nuget": {"push", "delete"},
    "terraform": {"apply", "destroy", "import", "taint"},
    "tofu": {"apply", "destroy", "import", "taint"},
    "pulumi": {"up", "destroy", "import", "refresh"},
    "kubectl": {
        "apply",
        "create",
        "delete",
        "replace",
        "patch",
        "scale",
        "rollout",
        "drain",
        "cordon",
        "edit",
        "set",
        "label",
        "annotate",
        "exec",
    },
    "helm": {"install", "upgrade", "uninstall", "delete", "rollback"},
    "firebase": {"deploy"},
    "netlify": {"deploy"},
    "wrangler": {"deploy", "publish", "delete"},
    "flyctl": {"deploy", "destroy"},
    "fly": {"deploy", "destroy"},
    "vercel": {"deploy", "--prod", "remove", "rm"},
}
_GH_CHANGES = {
    "release": {"create", "delete", "edit", "upload"},
    "repo": {"create", "delete", "archive", "edit", "rename", "fork"},
    "pr": {"merge", "close"},
    "secret": {"set", "delete", "remove"},
    "variable": {"set", "delete"},
    "workflow": {"run", "enable", "disable"},
    "api": {"--method", "-X"},
}
_CLOUD_CLIS = {"aws", "gcloud", "az", "doctl", "heroku", "railway"}
_CLOUD_READ_VERBS = {
    "describe",
    "list",
    "ls",
    "get",
    "show",
    "logs",
    "status",
    "whoami",
    "version",
    "help",
    "--version",
    "--help",
    "info",
    "config",
    "auth",
    "login",
    "account",
    "configure",
}
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def review_reasons(command: str, *, dialect: ShellDialect | None = None) -> list[str]:
    """Why ``command`` needs a decision in "Approve for me" (empty when it does not)."""

    reasons: list[str] = []
    for reason in _reasons(str(command or ""), dialect or host_dialect(), depth=0):
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _reasons(command: str, dialect: ShellDialect, *, depth: int) -> list[str]:
    if depth > _MAX_DEPTH:
        return []
    found: list[str] = []
    for item in split_simple_commands(command, dialect=dialect, strict=False) or []:
        words = list(item.words)
        while words and command_name(words[0]) in {"sudo", "doas", "command", "nohup", "call"}:
            words = words[1:]
        for redirect in item.redirects:
            if redirect.operator != "<" and names_credential_file(redirect.target):
                found.append(f"writes a credential file ({redirect.target})")
        if not words:
            continue
        unwrapped = unwrap_shell_payload(words)
        if unwrapped:
            payload, payload_dialect = unwrapped
            found += _reasons(payload, payload_dialect, depth=depth + 1)
            continue
        found += _simple_reasons(words)
    return found


def _simple_reasons(words: list[str]) -> list[str]:
    name = command_name(words[0])
    arguments = words[1:]
    found: list[str] = []
    credential = next((a for a in arguments if names_credential_file(a)), None)
    if credential is not None:
        found.append(f"uses a credential file ({credential})")
    if name == "git":
        reason = _git_reason(arguments)
        if reason:
            found.append(reason)
        return found
    if name == "python" or name == "python3" or name == "py":
        if len(arguments) >= 3 and arguments[0] == "-m" and arguments[1] == "twine":
            if arguments[2] == "upload":
                found.append("publishes a package (twine upload)")
        return found
    verbs = _PUBLISH.get(name)
    if verbs is not None:
        if name == "vercel" and not arguments:
            found.append("deploys (vercel)")
        elif any(a.lower() in verbs for a in arguments[:3]):
            verb = next(a for a in arguments[:3] if a.lower() in verbs)
            found.append(f"publishes or changes shared systems ({name} {verb})")
        return found
    if name == "gh" and arguments:
        group = arguments[0].lower()
        changes = _GH_CHANGES.get(group)
        if changes and any(a in changes for a in arguments[1:]):
            found.append(f"changes GitHub ({' '.join(words[:3])})")
        return found
    if name in _CLOUD_CLIS and arguments:
        if not any(
            a.lower() in _CLOUD_READ_VERBS or a.lower().startswith(("describe", "list", "get"))
            for a in arguments[:4]
        ):
            found.append(f"changes cloud resources ({name})")
    return found


def _git_reason(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in _GIT_GLOBAL_WITH_VALUE else 1
    if index >= len(arguments):
        return None
    subcommand = arguments[index]
    options = arguments[index + 1 :]
    flags = {o.split("=", 1)[0] for o in options if o.startswith("-")}
    short = "".join(o[1:] for o in options if o.startswith("-") and not o.startswith("--"))
    positionals = [o for o in options if not o.startswith("-")]
    if subcommand == "reset" and flags & {"--hard", "--merge", "--keep"}:
        return "discards uncommitted changes (git reset --hard)"
    if subcommand == "clean" and ("f" in short or "--force" in flags):
        return "deletes untracked files (git clean -f)"
    if subcommand == "checkout" and (
        "--" in options or "." in positionals or flags & {"-f", "--force"}
    ):
        return "discards uncommitted changes (git checkout -- …)"
    if subcommand == "restore" and not (
        {"--staged", "-S"} & flags and not {"--worktree", "-W"} & flags
    ):
        return "discards uncommitted changes (git restore)"
    if subcommand == "switch" and flags & {"-f", "--force", "--discard-changes"}:
        return "discards uncommitted changes (git switch --discard-changes)"
    if subcommand == "branch" and (
        "D" in short or ({"-d", "--delete"} & flags and flags & {"-f", "--force"})
    ):
        return "deletes a branch (git branch -D)"
    if subcommand == "stash" and positionals[:1] in (["drop"], ["clear"]):
        return "deletes stashed changes (git stash drop/clear)"
    if subcommand in {"filter-branch", "filter-repo"}:
        return f"rewrites history (git {subcommand})"
    if subcommand == "update-ref" and "-d" in flags:
        return "deletes a ref (git update-ref -d)"
    if subcommand == "reflog" and positionals[:1] in (["expire"], ["delete"]):
        return "drops history (git reflog expire)"
    return None


# A capability card only grants sandbox access. These effects need a separate
# person decision, including Full access and --yes. This is a bounded command
# classifier: unresolved programs/configuration ask too, but an approved program
# is not thereby confined to particular repositories.
_GITHUB_MUTATIONS = {
    **_GH_CHANGES,
    "pr": {
        "create",
        "edit",
        "merge",
        "close",
        "reopen",
        "comment",
        "review",
        "ready",
        "lock",
        "unlock",
        "update-branch",
        "revert",
    },
    "issue": {
        "create",
        "edit",
        "close",
        "reopen",
        "comment",
        "delete",
        "transfer",
        "pin",
        "unpin",
        "lock",
        "unlock",
        "develop",
    },
    "release": {"create", "delete", "delete-asset", "edit", "upload"},
    "repo": {
        "create",
        "delete",
        "archive",
        "unarchive",
        "edit",
        "rename",
        "fork",
        "sync",
        "deploy-key",
        "autolink",
    },
    "run": {"cancel", "delete", "rerun"},
    "label": {"clone", "create", "delete", "edit"},
    "project": {
        "close",
        "copy",
        "create",
        "delete",
        "edit",
        "field-create",
        "field-delete",
        "item-add",
        "item-archive",
        "item-create",
        "item-delete",
        "item-edit",
        "link",
        "unlink",
        "mark-template",
    },
    "cache": {"delete"},
    "gpg-key": {"add", "delete"},
    "ssh-key": {"add", "delete"},
    "gist": {"create", "delete", "edit", "rename"},
}
_GH_READS = {
    "pr": {"list", "view", "status", "diff", "checks"},
    "issue": {"list", "view", "status"},
    "release": {"list", "view", "download"},
    "repo": {"list", "view"},
    "run": {"list", "view", "download"},
    "workflow": {"list", "view"},
    "secret": {"list"},
    "variable": {"list", "get"},
    "label": {"list"},
    "cache": {"list"},
    "gpg-key": {"list"},
    "ssh-key": {"list"},
    "gist": {"list", "view"},
    "project": {"list", "view", "field-list", "item-list"},
}


def _command_arguments(arguments: list[str], value_options: set[str]) -> list[str]:
    """Skip bounded global flags, preserving the subcommand and its arguments."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        if arguments[index] == "--":
            return arguments[index + 1 :]
        index += 2 if arguments[index] in value_options else 1
    return arguments[index:]


def _graphql_query_is_read(query: str, operation_name: str | None) -> bool:
    """Accept one bounded literal query, never infer safety by erasing raw data.

    Deliberately exclude variables, directives, fragments and block strings.
    A request outside this small grammar needs a person, not a guessed parse.
    """
    if not query or len(query) > 8_000:
        return False
    # Check the entire original field, including comments and string contents.
    # GraphQL names are ASCII; no Unicode word boundary or line-ending guesses.
    if {"mutation", "subscription"}.intersection(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", query)):
        return False
    if any(
        (char.isspace() and char not in " \t\r\n") or (ord(char) < 32 and char not in "\t\r\n")
        for char in query
    ):
        return False

    # Match at every character position; never skip an unrecognized character.
    # Comments stop at CR OR LF. Strings cannot swallow either raw terminator.
    lexer = re.compile(
        r"(?P<ignored>[ \t\r\n,]+|#[^\r\n]*)"
        r'|(?P<string>"(?:\\["\\/bfnrt]|\\u[0-9A-Fa-f]{4}|[^"\\\x00-\x1f])*")'
        r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
        r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?(?![A-Za-z_0-9.]))"
        r"|(?P<punct>[{}():\[\]])"
    )
    tokens: list[tuple[str, str]] = []
    offset = 0
    while offset < len(query):
        match = lexer.match(query, offset)
        if match is None:
            return False
        if match.group().startswith("#") and "query" in re.findall(
            r"[A-Za-z_][A-Za-z_0-9]*", match.group()
        ):
            return False  # operation keywords in comments also require a person
        if match.lastgroup != "ignored":
            tokens.append((str(match.lastgroup), match.group()))
        offset = match.end()
    index = 0

    def take(punctuation: str) -> bool:
        nonlocal index
        if index < len(tokens) and tokens[index] == ("punct", punctuation):
            index += 1
            return True
        return False

    def name() -> str:
        nonlocal index
        if index >= len(tokens) or tokens[index][0] != "name":
            raise ValueError("expected a literal GraphQL name")
        word = tokens[index][1]
        index += 1
        return word

    def value(depth: int) -> None:
        nonlocal index
        if depth > 32 or index >= len(tokens):
            raise ValueError("unresolved GraphQL value")
        if tokens[index][0] in {"name", "string", "number"}:
            index += 1
        elif take("["):
            while not take("]"):
                value(depth + 1)
        elif take("{"):
            while not take("}"):
                name()
                if not take(":"):
                    raise ValueError("unresolved GraphQL object field")
                value(depth + 1)
        else:
            raise ValueError("unresolved GraphQL value")

    def selection(depth: int) -> None:
        if depth > 32 or not take("{"):
            raise ValueError("expected one GraphQL selection set")
        fields = 0
        while not take("}"):
            name()
            fields += 1
            if take(":"):
                name()
            if take("("):
                # At least one literal argument; variables are unsupported.
                while True:
                    name()
                    if not take(":"):
                        raise ValueError("unresolved GraphQL argument")
                    value(depth + 1)
                    if take(")"):
                        break
            if index < len(tokens) and tokens[index] == ("punct", "{"):
                selection(depth + 1)
        if not fields:
            raise ValueError("empty GraphQL selection set")

    try:
        selected_name = None
        if tokens and tokens[0] == ("name", "query"):
            index += 1
            if index < len(tokens) and tokens[index][0] == "name":
                selected_name = name()
        if operation_name is not None and operation_name != selected_name:
            return False
        selection(0)
        return index == len(tokens)  # no second operation or other definition
    except ValueError:
        return False


def _github_api_is_read(arguments: list[str]) -> bool:
    """REST defaults to GET; only a fully resolved single GraphQL query reads."""

    method = None
    endpoint = None
    fields: list[tuple[str, str]] = []
    has_input = False
    graphql_options_resolved = True
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        option, equal, value = arg.partition("=")
        valued = {
            "-X",
            "--method",
            "-f",
            "--raw-field",
            "-F",
            "--field",
            "--input",
            "-H",
            "--header",
            "--hostname",
            "--jq",
            "-q",
            "--template",
            "-t",
            "--cache",
        }
        # gh accepts attached short values (-XPOST, -fkey=value).
        if arg[:2] in {"-X", "-f", "-F", "-H", "-q", "-t"} and len(arg) > 2:
            option, value, equal = arg[:2], arg[2:], "="
        if option in valued:
            if not equal:
                index += 1
                if index >= len(arguments):
                    return False
                value = arguments[index]
            if option in {"-X", "--method"}:
                if method is not None:
                    return False
                method = value.upper()
            elif option in {"-f", "--raw-field", "-F", "--field"}:
                fields.append((option, value))
            elif option == "--input":
                has_input = True
            else:
                graphql_options_resolved = False
        elif arg.startswith("-"):
            if arg not in {"--paginate", "--slurp", "--silent", "--include", "-i", "--verbose"}:
                return False
        elif endpoint is None:
            endpoint = arg
        else:
            return False
        index += 1
    if endpoint is None or has_input:
        return False
    # Only literal API paths enter the bounded read exceptions. URL/query-string
    # payloads, repository placeholders, encoded paths and dot segments must not route a GraphQL request
    # through the REST GET exception without inspecting its original document.
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", endpoint) or any(
        part in {".", ".."} for part in endpoint.split("/")
    ):
        return False
    if endpoint.strip("/") == "graphql":
        if not graphql_options_resolved or method not in {None, "GET", "POST"}:
            return False
        literal_fields: dict[str, str] = {}
        for option, field in fields:
            key, equal, value = field.partition("=")
            # Typed fields can load files/stdin or substitute repository context.
            # Duplicate, nested, variable and unresolved fields need a person.
            if (
                option not in {"-f", "--raw-field"}
                or not equal
                or key not in {"query", "operationName"}
                or key in literal_fields
            ):
                return False
            literal_fields[key] = value
        return _graphql_query_is_read(
            literal_fields.get("query", ""), literal_fields.get("operationName")
        )
    return method in {"GET", "HEAD"} or (method is None and not fields)


# A deliberately narrower grammar than the general read-only convenience rule.
# Tools that can select executable helpers/configuration (find, sort, rg, file,
# language/build tools, etc.) stay opaque here. Unknown options also ask.
_LITERAL_READ_PROGRAMS = frozenset(
    {
        "echo",
        "printf",
        "pwd",
        "true",
        "false",
        "cat",
        "head",
        "tail",
        "wc",
        "ls",
        "basename",
        "dirname",
        "uname",
        "id",
        "whoami",
        "readlink",
        "realpath",
        "stat",
        "cut",
        "tr",
        "grep",
    }
)


def _active_posix_expansion_or_escape(text: str) -> bool:
    """Recognize active metacharacters without reading variables or evaluating code."""
    quote = ""
    for char in text:
        if quote == "'":
            if char == "'":
                quote = ""
            continue
        if char in {"$", "`", "\\"} or (not quote and char in {"*", "?", "[", "~"}):
            return True
        if char == '"':
            quote = "" if quote == '"' else '"'
        elif char == "'" and not quote:
            quote = "'"
    return False


def github_write_reason(
    command: str,
    *,
    credential_access: bool = False,
    opaque_authentication_inputs: bool = False,
    dialect: ShellDialect | None = None,
) -> str | None:
    """Mandatory person decision for remote writes or unresolved command effects.

    Only bounded literal reads/data avoid this card. Unknown programs, aliases,
    configuration and active shell expansion never become an implicit allow,
    even without a new typed credential grant. Authentication remains separate.
    """
    from .read_only_commands import is_read_only_command

    def inspect(text: str, shell: ShellDialect, depth: int) -> tuple[str | None, bool]:
        if depth > _MAX_DEPTH:
            return "Nested shell parsing limit requires a separate person approval", False
        if shell == "posix" and _active_posix_expansion_or_escape(text):
            return "Unresolved shell expansion or escape requires a separate person approval", False
        # PowerShell can resolve even familiar cmdlet/executable spellings to
        # session aliases/functions, and invoke code through expression/process
        # cmdlets. This gate has no trusted resolution/profile snapshot. Keep
        # the whole original operation behind a person instead of guessing from
        # argv, including nested PowerShell payloads and literal-looking reads.
        if shell == "powershell" and text.strip():
            return (
                "Unverified PowerShell command resolution requires a separate person approval",
                False,
            )
        if shell == "cmd" and any(marker in text for marker in ("%", "!", "^")):
            # Percent/delayed expansion and caret continuations can change the
            # executable, verb or separators. Do not expand user environment.
            return "Unresolved Windows shell expansion requires a separate person approval", False
        parsed = split_simple_commands(text, dialect=shell, strict=True)
        if parsed is None:
            # Ambiguous shell syntax cannot bypass the mandatory gate even
            # without a newly requested credential capability (Auto/yes/Full).
            return "Unresolved shell syntax requires a separate person approval", False
        certain = True
        for item in parsed:
            words = list(item.words)
            # Literal environment assignments and common executable wrappers do
            # not hide the command they launch. Unsupported options stay opaque.
            while words:
                first = words[0]
                key, equal, _ = first.partition("=")
                if equal and key.replace("_", "a").isalnum() and not key[0].isdigit():
                    certain = False  # assignments can configure executable helpers
                    words.pop(0)
                elif command_name(first) in {
                    "command",
                    "exec",
                    "nohup",
                    "call",
                    "env",
                    "sudo",
                    "doas",
                }:
                    certain = False  # wrapper options/environment may change execution
                    words.pop(0)
                    # Wrapper flags differ across platforms and can consume the
                    # executable-looking word (e.g. exec -a NAME). Do not guess
                    # their arity and accidentally certify the remainder.
                    if any(word.startswith("-") for word in words):
                        return (
                            "Unresolved executable wrapper options require a separate person approval",
                            False,
                        )
                else:
                    break
            if not words:
                certain = False
                continue
            unwrapped = unwrap_shell_payload(words)
            if unwrapped:
                # Startup files and wrapper flags can execute code before the
                # visible payload. Unwrap to find writes, never to prove a read.
                certain = False
                reason, readable = inspect(unwrapped[0], unwrapped[1], depth + 1)
                if reason:
                    return reason, False
                certain &= readable
                continue
            if unwrapped is False:
                return "Unresolved shell payload requires a separate person approval", False
            name = command_name(words[0])
            if shell == "cmd" and name in {"git", "gh"}:
                # Git Credential Manager/wincred and other executable config or
                # CLI aliases are not proven harmless by a literal read verb.
                # This applies even without a newly requested credential grant.
                return (
                    "Windows Git/GitHub configuration effects require a separate person approval",
                    False,
                )
            if shell == "posix" and name in {
                "if",
                "then",
                "else",
                "elif",
                "fi",
                "for",
                "while",
                "until",
                "do",
                "done",
                "case",
                "esac",
                "!",
                "time",
            }:
                return "Unresolved shell control flow requires a separate person approval", False
            args = (
                _command_arguments(words[1:], _GIT_GLOBAL_WITH_VALUE | {"--config-env"})
                if name == "git"
                else words[1:]
            )
            if name in {"git-push", "git-send-pack", "git-http-push"}:
                return "Git remote writes require a separate person approval", False
            if name == "git":
                if args and args[0] in {"push", "send-pack", "http-push"}:
                    return "Git remote writes require a separate person approval", False
                # Even literal ls-remote consumes effective Git configuration:
                # local/worktree/includes, helpers, URL rewrites and environment
                # may select opaque programs. No argv-only credentialed read
                # exception until the consumer enforces a configuration boundary.
                # Config may execute code even without a new credential grant.
                return "Git configuration effects require a separate person approval", False
            elif name == "gh":
                if args and args[0].startswith("-"):
                    return (
                        "Unresolved GitHub global options require a separate person approval",
                        False,
                    )
                args = _command_arguments(args, {"-R", "--repo", "--hostname"})
                if not args:
                    readable = False
                elif args[0] == "api":
                    readable = _github_api_is_read(args[1:])
                    if not readable:
                        return (
                            "GitHub API writes or unresolved effects require a separate person approval",
                            False,
                        )
                else:
                    if len(args) > 1 and args[1].startswith("-"):
                        return (
                            "Unresolved GitHub command options require a separate person approval",
                            False,
                        )
                    subcommand = _command_arguments(args[1:], {"-R", "--repo", "--hostname"})
                    if subcommand and subcommand[0] in _GITHUB_MUTATIONS.get(args[0], set()):
                        return "GitHub mutations require a separate person approval", False
                    readable = bool(
                        subcommand
                        and subcommand[0] in _GH_READS.get(args[0], set())
                        and all(not arg.startswith("-") for arg in subcommand[1:])
                    )
            else:
                readable = (
                    name in _LITERAL_READ_PROGRAMS
                    and (name == "echo" or not any(arg.startswith("-") for arg in words[1:]))
                    and is_read_only_command(text, dialect=shell)
                )
            if shell == "cmd" and not readable:
                return (
                    "Unresolved Windows command effects require a separate person approval",
                    False,
                )
            certain &= readable and not item.redirects and words[0] == name
        return None, certain

    reason, readable = inspect(str(command or ""), dialect or host_dialect(), 0)
    if reason:
        return reason
    if not readable:
        return "Unresolved command effects require a separate person approval"
    if credential_access and opaque_authentication_inputs:
        return "This credential-bearing command has unresolved effects and requires a separate person approval"
    return None
