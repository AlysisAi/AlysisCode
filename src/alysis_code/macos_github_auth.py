"""Read a saved GitHub login for one reviewed, contained Mac Git operation.

Only the installed credential reader runs on the host. Repository Git, helpers
and hooks remain under Seatbelt. The credential has full account authority.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from .command_text import command_text
from .config import ConfigError
from .git_github_auth import REMOTE_VERBS, GithubCredential
from .macos_git import literal_git_argv


def validate_command(command: str) -> None:
    argv = literal_git_argv(command)
    arguments = argv[1:] if argv else []
    while arguments:
        if arguments[0] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
            arguments = arguments[1:]
        elif arguments[0] == "-C" and len(arguments) > 1:
            arguments = arguments[2:]
        else:
            break
    if not arguments or arguments[0] not in REMOTE_VERBS:
        raise ConfigError(
            "GitHub authentication requires one literal git clone/fetch/pull/push/ls-remote "
            "command, without shell chaining or executable configuration overrides."
        )


def credential_reader() -> Path:
    import grp

    # Do not trust PATH, a repository helper, or environment-selected executables.
    # Homebrew's Cellar is normally group-writable by macOS administrators.
    privileged_groups = {0, grp.getgrnam("admin").gr_gid}
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        try:
            executable = (prefix / "bin/gh").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not executable.is_relative_to(prefix) or not executable.is_file():
            continue
        if not os.access(executable, os.X_OK):
            continue
        for path in (executable, *executable.parents):
            info = path.stat()
            if (
                info.st_uid not in (0, os.getuid())
                or info.st_mode & stat.S_IWOTH
                or (info.st_mode & stat.S_IWGRP and info.st_gid not in privileged_groups)
            ):
                raise ConfigError("GitHub CLI installation is not trusted for credential access.")
        return executable
    raise ConfigError(
        "GitHub authentication needs GitHub CLI installed in /opt/homebrew or /usr/local "
        "and an existing gh auth login for github.com."
    )


def load_credential(*, workspace_roots: tuple[Path, ...] = ()) -> GithubCredential:
    import pwd

    executable = credential_reader()
    if any(executable.is_relative_to(root.resolve()) for root in workspace_roots):
        raise ConfigError("The credential reader cannot be inside an approved writable workspace.")
    # Use the OS user's standard login, including when Desktop has an isolated
    # profile. Ignore GH_TOKEN, GH_CONFIG_DIR, XDG and dynamic-loader overrides.
    home = pwd.getpwuid(os.getuid()).pw_dir
    environment = {
        "HOME": home,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
    }
    try:
        result = subprocess.run(
            [str(executable), "auth", "token", "--hostname", "github.com"],
            cwd=executable.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **command_text(),
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("Could not read the saved GitHub CLI login for this operation.") from None
    token = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9_]{20,256}", token):
        raise ConfigError("No usable saved github.com login. Run gh auth login, then retry.")
    return GithubCredential(token)
