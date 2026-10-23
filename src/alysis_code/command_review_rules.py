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
Full access runs them. ``git push`` and package installs were already on that list.

Matching parses the command (``shell_command_lexer``) instead of searching its text, so
``echo "git reset --hard"`` is not a reset and ``cmd /c "git clean -fdx"`` is.
"""

from __future__ import annotations

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
