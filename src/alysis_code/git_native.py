"""Literal Git commands in a native job with repository write access.

Unlike the general shell, this entry point receives Git metadata writes: the
repository's objects, refs and index. What decides which programs a later
Git runs (configuration, hooks, the state of a rebase and the rest of
``program_entries``) it receives only for a command a person decided on.
Hooks, filters, signing programs and Git's children inherit this same OS
containment; none execute on the host.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
# Verbs whose whole work is what a later Git runs: the repository's
# configuration and its remotes. The Git job takes them only for a command a
# person approved ``permissions.git_metadata`` for. Without it they run in the
# general shell like any other program, where they read and cannot write.
METADATA_VERBS = frozenset({"config", "remote"})


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


def native_argv(
    command: str, *, inspection: bool = False, metadata_access: bool = False
) -> list[str] | None:
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
    if inspection:
        verbs = READ_VERBS
    else:
        verbs = VERBS | METADATA_VERBS if metadata_access else VERBS
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


def shell_git_route(command: str, *, metadata_access: bool = False) -> tuple[str, list[str]] | None:
    """How the shell tool runs a literal Git command under MXC, with its argv.

    ``native-read`` and ``reader`` only inspect: the first is real Git in a job
    where nothing is writable, the second the fixed read worker. ``native-write``
    is the one route that receives writable repository metadata. Everything
    else, including Git reached through another program, runs in the general
    shell with the metadata write-protected. The shell tool dispatches on this
    answer and the command guard asks it too, so the two cannot disagree.

    ``metadata_access`` says a person approved ``permissions.git_metadata``
    for the command. The write route then also takes ``METADATA_VERBS``.
    """
    from .git_shell_inspection import inspection_argv

    if (argv := native_argv(command, inspection=True)) is not None:
        return "native-read", argv
    if (argv := inspection_argv(command)) is not None:
        return "reader", argv
    if (argv := native_argv(command, metadata_access=metadata_access)) is not None:
        return "native-write", argv
    return None


def git_metadata_writes(command: str) -> bool:
    """Whether the shell tool hands this command writable repository metadata."""
    route = shell_git_route(command)
    return route is not None and route[0] == "native-write"


def takes_metadata_permission(command: str) -> bool:
    """Whether ``permissions.git_metadata`` changes anything for this command.

    It does for a literal Git command the Git job runs with writable
    metadata. For an inspection, and for Git reached through a script or
    another program, it would be approved and then change nothing.
    """
    route = shell_git_route(command, metadata_access=True)
    return route is not None and route[0] == "native-write"


# The entries of a Git directory that are folders, among those that decide
# what a later Git runs (``macos_git.GIT_PROGRAM_ENTRIES``).
_PROGRAM_FOLDERS = frozenset({"hooks", "rebase-merge", "remotes", "branches"})
_CANNOT_KEEP = (
    "Git's configuration and hooks cannot be kept as they are in this repository right now, "
    "so the command was not run. Another program may have one of them open: try again. If it "
    "stays so (one of them is a link, say), request operation-only "
    "permissions.git_metadata=true for the command, which a person approves."
)
# The names that mean something to Git already as an empty entry: it stops at
# an empty "commondir" and takes an empty "rebase-merge" folder for a rebase
# in progress. Every other name can stand empty while a command runs.
_NEVER_EMPTY = frozenset({"commondir", "rebase-merge"})


@dataclass(frozen=True)
class ProgramEntries:
    """What a Git command may not change in its Git directories, by how each is kept.

    The sandbox refuses a path only when it exists. ``existing`` entries are
    write-protected as they are. ``empty`` names do not exist and Git does not
    mind an empty entry there: one is made for the command, write-protected
    and removed afterwards. ``taken`` names do not exist and must not look as
    if they did: the engine takes the name for as long as the command runs
    (``_windows_git_reservations.HeldNames``). Both carry whether the name is
    a folder.

    ``read_only`` holds a folder of worktrees that contains this command's
    own Git directory, which is in ``own``. The folder is write-protected by
    a rule, which also covers a worktree somebody adds meanwhile, and nothing
    holds it open: the Git of each other worktree replaces its index and
    ``HEAD`` in there while this command runs. Inside it only ``own`` is
    writable.
    """

    existing: tuple[Path, ...]
    empty: tuple[tuple[Path, bool], ...]
    taken: tuple[tuple[Path, bool], ...]
    read_only: tuple[Path, ...]
    own: tuple[Path, ...]


def _after_global_options(argv: list[str]) -> list[str]:
    """The verb and its arguments, without the options ``run`` accepts before the verb."""
    words = list(argv)
    while words:
        if words[0] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
            words.pop(0)
        elif words[0] in {"-C", "-c"} and len(words) > 1:
            del words[:2]
        else:
            break
    return words


def creates_plain_repository(argv: list[str]) -> bool:
    """Whether the words are a plain ``git init`` and nothing else.

    Creating a repository writes its first configuration and sample hooks.
    Without a template, a configuration option or a separate Git directory,
    Git chooses every byte of both itself and starts no other program, so
    nothing in the command decides what a later Git runs. The same question
    ``macos_git.git_creates_repository`` answers for a POSIX command.
    """
    words = _after_global_options(argv)
    if words[:1] != ["init"] or any(word == "-c" for word in argv):
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


def programs_exposed(command: str, settings) -> bool:
    """Whether the Git job runs this command able to change what a later Git runs.

    A literal Git command that changes the repository writes its objects, refs
    and index. Configuration, hooks, a rebase in progress and the rest of
    ``GIT_PROGRAM_ENTRIES`` stay as they are (``program_entries``), unless a
    person approved ``permissions.git_metadata`` for the command. Two verbs
    cannot run that way, because the repository they write does not exist
    yet: ``clone``, and ``init`` when it is more than a plain one. The
    command guard asks this function. The Git job never gives a command
    more than this says: it reads the words that follow the options it
    takes itself, and for a plain ``init`` it also looks at what is in the
    folder (``_init_target``).
    """
    approved = bool(getattr(settings, "git_metadata_access", False))
    route = shell_git_route(command, metadata_access=approved)
    if route is None or route[0] != "native-write":
        return False
    verb = _after_global_options(route[1])[:1]
    if verb == ["init"]:
        return not creates_plain_repository(route[1])
    return verb == ["clone"] or approved


def writes_a_remote(command: str) -> bool:
    """Whether the Git job runs this command to change a remote: a literal ``git push``.

    It is asked about because what it changes is not in the sandbox, and a
    command a person decided on gets the whole Git directory as before.
    """
    route = shell_git_route(command)
    return (
        route is not None
        and route[0] == "native-write"
        and _after_global_options(route[1])[:1] == ["push"]
    )


def program_entries(metadata: list[Path]) -> ProgramEntries:
    """What in these Git directories must not change, and what must not appear.

    Staging, committing and branching write objects, refs and the index.
    They do not write what decides which programs a later Git runs: the
    names of ``GIT_PROGRAM_ENTRIES``, and the Git directories of submodules
    and of other worktrees. Those two folders are protected as a whole,
    apart from this command's own Git directory where it lies in one.
    """
    from .macos_git import GIT_PROGRAM_ENTRIES

    folders = list(dict.fromkeys(metadata))
    existing: list[Path] = []
    empty: list[tuple[Path, bool]] = []
    taken: list[tuple[Path, bool]] = []
    read_only: list[Path] = []
    own: list[Path] = []
    for folder in folders:
        for name in (*GIT_PROGRAM_ENTRIES, "modules", "worktrees"):
            path = folder / name
            inside = [other for other in folders if other != folder and other.is_relative_to(path)]
            if not os.path.lexists(path):
                absent = taken if name in _NEVER_EMPTY else empty
                absent.append((path, name in _PROGRAM_FOLDERS or name in {"modules", "worktrees"}))
            elif inside:
                read_only.append(path)
                own.extend(inside)
            else:
                existing.append(path)
    return ProgramEntries(tuple(existing), tuple(empty), tuple(taken), tuple(read_only), tuple(own))


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
    try:
        shared = common.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        shared = None
    except PermissionError:
        # A Git command that is running keeps this name from being created
        # (HeldNames): Windows lists it and refuses to open it. Only a Git
        # directory with its own objects does without the file.
        if not (metadata / "objects").is_dir():
            raise
        shared = None
    if shared is not None:
        _local_path(common, directory=False)
        result.append(_local_path((metadata / shared).resolve()))
    for path in result:
        if not any(path.is_relative_to(root) for root in roots):
            raise ConfigError(
                "Linked Git metadata is outside the approved folders. Add its repository folder first."
            )
    return result


def _init_target(argv: list[str], cwd: Path, roots: list[Path]) -> tuple[Path, bool]:
    """The folder a plain ``git init`` works in, and whether its repository is new.

    Git chooses every byte of a new repository's configuration and hooks.
    Where a Git directory exists with anything in it, ``init`` reads and
    runs what is there (a hook on the first reference, say), so it is a
    command on that repository like any other.
    """
    words = _after_global_options(argv)[1:]
    folders = []
    index = 0
    while index < len(words):
        if words[index] in ("-b", "--initial-branch"):
            index += 1
        elif not words[index].startswith("-"):
            folders.append(words[index])
        index += 1
    target = (cwd / folders[0]).absolute() if folders else cwd
    if ".." in target.parts or not any(target.is_relative_to(p) for p in roots):
        raise ConfigError("Create the repository inside the approved folders.")
    own = target / ".git"
    try:
        entry = os.lstat(own)
    except FileNotFoundError:
        return target, True
    if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
        return target, False  # a link: whatever it leads to is not new
    # An empty folder is the entry that reserves the name, not a repository.
    return target, stat.S_ISDIR(entry.st_mode) and not any(own.iterdir())


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
    *,
    settings,
    root: Path,
    cwd: Path,
    argv: list[str],
    timeout_s,
    cancel_check,
    inspection=False,
    whole_metadata=False,
):
    """Run one literal Git command.

    ``whole_metadata`` says a person decided on this command: it then gets
    the whole Git directory, as ``settings.git_metadata_access`` gives it.
    """
    whole_metadata = bool(whole_metadata or settings.git_metadata_access) and not inspection
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
    accepted = READ_VERBS if inspection else VERBS
    if not inspection and settings.git_metadata_access:
        accepted = accepted | METADATA_VERBS
    if not argv or argv[0] not in accepted:
        raise ConfigError("Unsupported literal Git operation")
    if (
        not inspection
        and argv[0] == "config"
        and any(a in {"--global", "--system"} for a in argv[1:])
    ):
        # The job gives Git a copy of the person's preferences, not the file.
        raise ConfigError(
            "permissions.git_metadata covers this repository's own configuration. The Git "
            "configuration of the person's account and of the machine is theirs to change."
        )
    if settings.github_auth:
        from .git_github_auth import REMOTE_VERBS
        from .sandbox_runner import select_shell_sandbox_backend

        select_shell_sandbox_backend(settings)
        if argv[0] not in REMOTE_VERBS:
            raise ConfigError(
                "GitHub authentication is limited to the approved remote Git operation."
            )
    destination = clone_destination(argv, cwd, roots) if argv[0] == "clone" else None
    new_repository = None
    if argv[0] == "init" and not whole_metadata:
        if not creates_plain_repository(argv):
            raise ConfigError(
                "This git init decides the new repository's configuration, hooks or location. "
                "Request operation-only permissions.git_metadata=true for it, which a person "
                "approves, or run a plain git init."
            )
        target, new = _init_target(argv, cwd, roots)
        # Nothing of a repository that exists is given to a command that
        # creates another one, also when it is created inside it.
        new_repository = target / ".git" if new else None
        if not new and not (target / ".git").exists():
            # A link that leads nowhere: looking further up for a repository
            # would find the one around this folder.
            raise ConfigError("The .git entry of that folder is a link that leads nowhere.")
        metadata = [] if new else repository_metadata(target, roots)
    else:
        metadata = [] if destination else repository_metadata(cwd, roots)
    if argv[0] not in {"init", "clone", "ls-remote"} and not metadata:
        raise ConfigError("The selected folder is not a Git repository")
    # Whether this command may change what a later Git runs: only where a
    # person decided on it, or where the repository does not exist yet.
    programs_protected = (
        not inspection
        and not whole_metadata
        and argv[0] != "clone"
        and not (argv[0] == "init" and new_repository)
    )
    # Only an inspection asks where the repository's folder is. (For an
    # init the repository can lie below the working folder, where a walk
    # upwards would never find it.)
    repository_root = cwd
    while (
        inspection
        and metadata
        and not (repository_root / ".git").exists()
        and repository_root != repository_root.parent
    ):
        repository_root = repository_root.parent
    linked_inspection = inspection and metadata and (repository_root / ".git").is_file()
    resources = ExitStack()
    credential = None
    try:
        for workspace in sorted(roots, key=lambda p: str(p).casefold()):
            lease = GitLease(workspace, timeout_s, cancel_check)
            resources.callback(lease.close)
        if metadata and not inspection:
            # Worktrees are projects of their own and share one repository.
            # What a command keeps in place there (program_entries) is kept
            # for one command at a time, whichever worktree it runs in.
            lease = GitLease(metadata[-1], timeout_s, cancel_check)
            resources.callback(lease.close)
        # Names a background command reserved and no command holds any more
        # go before Git looks at the folder, or "add -A" would stage them.
        from .windows_mxc import _take_back_reservations

        _take_back_reservations(roots)
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
            if new_repository:
                unlocked.append(new_repository)
            elif argv[0] == "init" and whole_metadata:
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
            entries = program_entries(metadata) if programs_protected else None
            if entries is not None:
                from ._windows_git_reservations import HeldNames

                names = HeldNames()
                resources.callback(names.close)
                appeared = []
                for path, directory in entries.taken:
                    try:
                        names.hold(path, directory=directory)
                    except FileExistsError:
                        appeared.append(path)  # since it was looked for
                    except OSError as exc:
                        raise ConfigError(_CANNOT_KEEP) from exc
                request["deny_write"].extend(str(path) for path in (*entries.existing, *appeared))
                # A missing folder is named with its separator, which is how
                # the reservations below know to make a folder.
                request["deny_write"].extend(
                    str(path) + (os.sep if directory else "") for path, directory in entries.empty
                )
            protect_private_state(request)
            for workspace in [] if inspection else roots:
                try:
                    reservations.reserve(workspace, request["deny_write"])
                except (ValueError, NotADirectoryError, PermissionError) as exc:
                    if entries is None:
                        raise
                    # One of the names is a link, a file where Git keeps a
                    # folder, or open in another program: the sandbox cannot
                    # keep it as it is by its path.
                    raise ConfigError(_CANNOT_KEEP) from exc
            # The safety reservations are infrastructure, not source files for
            # add -A / stash --include-untracked. Only our exclusive creations
            # are excluded, and those a running background command made and
            # that are still empty; existing user files are never hidden from Git.
            from .windows_mxc_reservations import recorded_files

            reserved = [p for p, *_ in reservations.created]
            for workspace in roots:
                reserved.extend(p for p in recorded_files(workspace) if p not in reserved)
            exclusions.write_text(
                "".join(
                    "/" + p.relative_to(cwd).as_posix() + "\n"
                    for p in reserved
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
                # Git's cache of folder listings would show it the names the
                # engine holds in the Git directory as if they were entries.
                "-c",
                "core.fscache=false",
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
            if entries is not None:
                # A read rule inside a writable folder refuses writing there
                # and holds nothing open; a writable folder inside it stays
                # writable. A deny would also refuse the folder inside.
                request["read"] = list(
                    dict.fromkeys([*request["read"], *(str(path) for path in entries.read_only)])
                )
                request["write"] = list(
                    dict.fromkeys([*request["write"], *(str(path) for path in entries.own)])
                )
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
        if programs_protected:
            # Git reports what it was refused itself and can still exit 0 (a
            # rebase does), so the policy that applied and what lifts it
            # accompany every outcome.
            completed.sandbox_policy = {
                "backend": "windows-mxc",
                "git_metadata": (
                    "writable except configuration, hooks and rebase state; a command that "
                    "needs those takes operation-only permissions.git_metadata=true"
                ),
            }
            if completed.returncode:
                completed.sandbox_policy["guidance"] = (
                    "Policy context, not a diagnosis of this exit code: this Git command could "
                    "write the repository's objects, refs and index, but not its configuration, "
                    "hooks, a rebase in progress, or the Git directories of submodules and other "
                    "worktrees. A command that needs those (rebase, upstream tracking, renaming "
                    "a branch, submodule or worktree setup) needs operation-only "
                    "permissions.git_metadata=true, which a person approves. A branch from a "
                    "remote branch needs none with --no-track. Inspect partial effects before "
                    "retrying."
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
