"""Approved literal Git commands in a native job with repository write access.

Unlike the general shell, this entry point receives Git metadata writes. The
exact Git argv is reviewed before dispatch. Hooks, filters, signing programs
and Git's children inherit this same OS containment; none execute on the host.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from .config import ConfigError
from .git_shell_inspection import literal_git_argv

VERBS = frozenset(
    {
        "switch",
        "checkout",
        "commit",
        "add",
        "branch",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "reset",
        "restore",
        "stash",
        "tag",
        "worktree",
        "submodule",
        "rm",
        "mv",
        "clean",
        "init",
        "fetch",
        "pull",
        "push",
        "clone",
    }
)
READ_VERBS = frozenset(
    {
        "log",
        "show",
        "rev-parse",
        "rev-list",
        "cat-file",
        "show-ref",
        "for-each-ref",
        "ls-tree",
        "merge-base",
        "describe",
        "branch",
        "diff",
        "config",
        "ls-remote",
    }
)


def safe_global_config(destination: Path):
    """Carry identity/format preferences, never credentials or executable hooks."""
    from dulwich.config import ConfigFile

    from .windows_toolchain import installed_tools

    result = ConfigFile()
    keys = [
        ("user", "name"),
        ("user", "email"),
        ("user", "signingkey"),
        ("core", "autocrlf"),
        ("core", "eol"),
        ("core", "safecrlf"),
        ("commit", "gpgsign"),
        ("tag", "gpgsign"),
        ("gpg", "format"),
        ("gpg.ssh", "program"),
        ("i18n", "commitencoding"),
    ]
    tools = installed_tools()
    if "git-sign" in tools:
        result.set((b"gpg", b"ssh"), b"program", str(tools["git-sign"][0]).encode())
    sources = ([tools["git"][1] / "etc/gitconfig"] if "git" in tools else []) + [
        Path.home() / ".config/git/config",
        Path.home() / ".gitconfig",
    ]
    for source in sources:
        if not source.is_file():
            continue
        config = ConfigFile.from_path(source)
        for section, name in keys:
            try:
                value = config.get(
                    tuple(part.encode() for part in section.split(".")), name.encode()
                )
            except KeyError:
                continue
            if len(value) > 4096 or b"\0" in value:
                raise ConfigError("Invalid global Git preference")
            result.set(tuple(part.encode() for part in section.split(".")), name.encode(), value)
    with destination.open("xb") as stream:
        result.write_to_file(stream)


def local_config_query_key(argv: list[str]) -> str | None:
    if len(argv) == 4 and argv[:3] == ["config", "--local", "--get"]:
        key = argv[3]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*\.[A-Za-z][A-Za-z0-9-]*", key):
            return key
    return None


def _ssh_config_argument(value: str) -> bool:
    key, separator, command = value.partition("=")
    return bool(separator and key.lower() == "core.sshcommand" and command.strip())


def native_argv(command: str, *, inspection: bool = False) -> list[str] | None:
    argv = literal_git_argv(command)
    if argv is None:
        return None
    position = 0
    while position < len(argv):
        if argv[position] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
            position += 1
        elif argv[position] == "-C" and position + 1 < len(argv):
            position += 2
        elif (
            argv[position] == "-c"
            and position + 1 < len(argv)
            and _ssh_config_argument(argv[position + 1])
        ):
            position += 2
        else:
            break
    verbs = READ_VERBS if inspection else VERBS
    if inspection and position < len(argv):
        verb, arguments = argv[position], argv[position + 1 :]
        if verb == "config" and local_config_query_key(argv) is None:
            return None
        if verb == "describe" and any(
            value.split("=", 1)[0] in {"--dirty", "--broken"} for value in arguments
        ):
            return None
        if verb == "diff" and not any(
            value in {"--cached", "--staged"}
            for value in arguments[: arguments.index("--") if "--" in arguments else len(arguments)]
        ):
            # Worktree directory opens are still incompatible with a read-only
            # MXC grant. Keep unstaged inspection on the contained reader.
            return None
        if verb == "branch" and arguments:
            first = arguments[0].split("=", 1)[0]
            read_flags = {
                "--list",
                "--show-current",
                "--all",
                "--remotes",
                "--verbose",
                "--contains",
                "--no-contains",
                "--merged",
                "--no-merged",
                "--points-at",
                "--sort",
                "--format",
                "--color",
                "--column",
                "--no-column",
                "--abbrev",
                "--no-abbrev",
            }
            if first not in read_flags and not (
                first.startswith("-") and len(first) > 1 and set(first[1:]) <= set("avr")
            ):
                return None
    return argv if position < len(argv) and argv[position] in verbs else None


def repository_metadata(cwd: Path, roots: list[Path]) -> list[Path]:
    from .windows_sandbox import _local_path

    current = cwd
    while not (current / ".git").exists():
        if current in roots or current == current.parent:
            return []
        current = current.parent
    marker = _local_path(current / ".git", directory=False)
    if marker.is_dir():
        metadata = marker
    else:
        value = marker.read_text(encoding="utf-8").strip()
        if not value.startswith("gitdir: ") or len(value) > 32768:
            raise ConfigError("Invalid linked Git directory")
        metadata = (current / value[8:]).absolute()
        if any(part == ".." for part in metadata.parts):
            metadata = metadata.resolve()
        metadata = _local_path(metadata)
    result = [metadata]
    common = metadata / "commondir"
    if common.exists():
        _local_path(common, directory=False)
        result.append(
            _local_path((metadata / common.read_text(encoding="utf-8").strip()).resolve())
        )
    for path in result:
        if not any(path.is_relative_to(root) for root in roots):
            raise ConfigError(
                "Linked Git metadata is outside the approved folders. Add its repository folder first."
            )
    return result


def clone_destination(argv: list[str], cwd: Path, roots: list[Path]) -> Path:
    """Start clones in a fresh child folder, never in an existing project root."""
    from .windows_sandbox import _local_path

    arguments = argv[1:]
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if len(arguments) != 2 or any(a.startswith("-") for a in arguments):
        raise ConfigError(
            "Use git clone -- <repository> <new-directory>, with an explicit new destination."
        )
    destination = (cwd / arguments[1]).absolute()
    if ".." in destination.parts or destination.exists():
        raise ConfigError("Clone into a new directory without parent traversal.")
    parent = _local_path(destination.parent)
    destination = parent / destination.name
    if not any(destination.is_relative_to(p) and destination != p for p in roots):
        raise ConfigError("Clone destination is outside the approved folders.")
    return destination


def run(
    *, settings, root: Path, cwd: Path, argv: list[str], timeout_s, cancel_check, inspection=False
):
    if not inspection and (
        settings.filesystem_access != "workspace-write" or not settings.safe_git_writes
    ):
        raise ConfigError("Git writes are disabled by this session's policy")
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ConfigError("Unsupported literal Git operation")
    if not inspection and any(
        a in {"-i", "--interactive", "-p", "--patch", "--edit-todo"} for a in argv[1:]
    ):
        raise ConfigError("Use a non-interactive Git operation with explicit arguments")
    # Refuse denied or malformed requests before loading native preparation.
    from ._windows_git_reservations import GitLease, GitReservations
    from .windows_mxc import WindowsMxcRunner
    from .windows_private_state import protect_private_state
    from .windows_toolchain import installed_tools

    tools = installed_tools()
    if "git" not in tools:
        raise ConfigError("The verified bundled Git runtime is unavailable; repair the app runtime")
    roots = list(
        dict.fromkeys(
            [root.resolve(), *(Path(p).resolve() for p in settings.additional_workspace_roots)]
        )
    )
    prefix = []
    argv = list(argv)
    while argv:
        if argv[0] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
            prefix.append(argv.pop(0))
        elif argv[0] == "-C" and len(argv) > 1:
            argv.pop(0)
            cwd = (cwd / argv.pop(0)).resolve()
            if not any(cwd.is_relative_to(p) for p in roots):
                raise ConfigError("Git working directory is outside the approved folders")
        elif argv[0] == "-c" and len(argv) > 1 and _ssh_config_argument(argv[1]):
            prefix.extend([argv.pop(0), argv.pop(0)])
        else:
            break
    if not argv or argv[0] not in (READ_VERBS if inspection else VERBS):
        raise ConfigError("Unsupported literal Git operation")
    if settings.github_auth:
        from .git_github_auth import REMOTE_VERBS
        from .sandbox_runner import select_shell_sandbox_backend

        select_shell_sandbox_backend(settings)
        if argv[0] not in REMOTE_VERBS:
            raise ConfigError(
                "GitHub authentication is limited to the approved remote Git operation."
            )
    destination = clone_destination(argv, cwd, roots) if argv[0] == "clone" else None
    metadata = [] if destination else repository_metadata(cwd, roots)
    if argv[0] not in {"init", "clone", "ls-remote"} and not metadata:
        raise ConfigError("The selected folder is not a Git repository")
    repository_root = cwd
    while metadata and not (repository_root / ".git").exists():
        repository_root = repository_root.parent
    linked_inspection = inspection and metadata and (repository_root / ".git").is_file()
    resources = ExitStack()
    credential = None
    try:
        for workspace in sorted(roots, key=lambda p: str(p).casefold()):
            lease = GitLease(workspace, timeout_s, cancel_check)
            resources.callback(lease.close)
        global_directory = resources.enter_context(
            tempfile.TemporaryDirectory(prefix="alysis-git-preferences-")
        )
        reservations = GitReservations()
        resources.callback(reservations.close)
        global_config = Path(global_directory) / "config"
        exclusions = Path(global_directory) / "exclude"
    except BaseException:
        resources.close()
        raise

    class GitRunner(WindowsMxcRunner):
        def _prepare(self, **kwargs):
            launcher, payload = super()._prepare(**kwargs)
            request = json.loads(payload)
            # Build and validate the private-state policy before broadening the
            # read-only base to the original, explicitly approved workspaces.
            request["write"] = [] if inspection else [str(p) for p in roots]
            request["read"].append(str(global_config))
            request["deny_write"].append(str(global_config))
            unlocked = [*metadata]
            if argv[0] == "init":
                unlocked.append(cwd / ".git")
            request["deny_write"] = (
                request["deny_write"]
                if inspection
                else [
                    p
                    for p in request["deny_write"]
                    if Path(p) not in roots and not any(Path(p) == m for m in unlocked)
                ]
            )
            protect_private_state(request)
            for workspace in [] if inspection else roots:
                reservations.reserve(workspace, request["deny_write"])
            # The safety reservations are infrastructure, not source files for
            # add -A / stash --include-untracked. Only our exclusive creations
            # are excluded; existing user files are never hidden from Git.
            exclusions.write_text(
                "".join(
                    "/" + p.relative_to(cwd).as_posix() + "\n"
                    for p, *_ in reservations.created
                    if p.is_relative_to(cwd)
                ),
                encoding="utf-8",
            )
            request["read"].append(str(exclusions))
            request["deny_write"].append(str(exclusions))
            env = dict(request["env"])
            env.update(
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL=str(global_config),
                GIT_CONFIG_SYSTEM="NUL",
                GIT_TERMINAL_PROMPT="0",
                GIT_PAGER="cat",
                GIT_EDITOR="true",
                GIT_SEQUENCE_EDITOR="false",
                GIT_MERGE_AUTOEDIT="no",
                HOME=str(cwd),
            )
            for key in list(env):
                if key.startswith("GIT_") and key not in {
                    "GIT_CONFIG_NOSYSTEM",
                    "GIT_CONFIG_GLOBAL",
                    "GIT_CONFIG_SYSTEM",
                    "GIT_TERMINAL_PROMPT",
                    "GIT_PAGER",
                    "GIT_EDITOR",
                    "GIT_SEQUENCE_EDITOR",
                    "GIT_MERGE_AUTOEDIT",
                }:
                    del env[key]
            # Git otherwise prefers its MSYS SSH, which cannot initialize under
            # PSEC. core.sshCommand remains available for explicitly configured
            # options; both it and this default client run inside the same job.
            openssh = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
            if openssh.is_file():
                env.update(GIT_SSH=str(openssh), GIT_SSH_VARIANT="ssh")
            request["env"] = sorted(env.items())
            if credential is not None:
                credential.configure(env)
                request["env"] = sorted(env.items())
            request["argv"] = [
                str(tools["git"][0]),
                # Avoid Git's linked-repository discovery chdir/fchdir cycle,
                # which requests a writable directory handle on Windows. Both
                # paths were validated above; the policy remains read-only.
                *(
                    [f"--git-dir={metadata[0]}", f"--work-tree={repository_root}"]
                    if linked_inspection
                    else []
                ),
                "--no-pager",
                *(["--no-optional-locks"] if inspection else []),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "gc.auto=0",
                "-c",
                "maintenance.auto=false",
                "-c",
                "credential.helper=",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=always",
                *(
                    ["-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always"]
                    if settings.network == "on"
                    else []
                ),
                "-c",
                f"core.excludesFile={exclusions}",
                *prefix,
                *argv,
            ]
            request["network"] = settings.network
            return launcher, json.dumps(request).encode()

    try:
        safe_global_config(global_config)
        if settings.github_auth:
            from .git_github_auth import load_credential

            credential = load_credential()
        completed = GitRunner(
            replace(settings, filesystem_access="read-only", github_auth=False)
        ).run(root=root, cwd=cwd, cmd="", timeout_s=timeout_s, cancel_check=cancel_check)
        completed.inspection_backend = (
            "contained-native-git-readonly" if inspection else "contained-native-git"
        )
        if credential is not None:
            completed.stdout = credential.redact(completed.stdout)
            completed.stderr = credential.redact(completed.stderr)
        key = local_config_query_key(argv) if inspection else None
        if key and completed.returncode == 1 and not completed.stdout and not completed.stderr:
            # Git documents exit 1 for a missing --get key. Preserve the process
            # status while distinguishing this observation from an execution error.
            completed.git_observation = {"kind": "local-config-key-unset", "key": key}
        return completed
    except BaseException as exc:
        # Cancellation/timeout errors also carry captured child output. Keep the
        # same secret filtering on those paths before the shell tool records it.
        if credential is not None:
            for name in ("stdout", "stderr", "output"):
                value = getattr(exc, name, None)
                if isinstance(value, str):
                    setattr(exc, name, credential.redact(value))
                elif isinstance(value, bytes):
                    setattr(
                        exc,
                        name,
                        credential.redact(value.decode("utf-8", errors="replace")).encode(),
                    )
        raise
    finally:
        resources.close()
