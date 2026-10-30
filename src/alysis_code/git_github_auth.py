"""Explicit GitHub sign-in handoff to one contained native Git command tree.

Only the fixed, machine-installed gh credential reader runs on the host. Git,
repository configuration, hooks and remote helpers still execute under MXC.
The account credential is available to that approved tree, not restricted to a
single repository. Admission must describe this authority, and cannot retain it.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass, field

from .command_text import command_text
from .config import ConfigError

REMOTE_VERBS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})


def validate_command(command: str) -> None:
    from .git_native import native_argv

    argv = native_argv(command) or native_argv(command, inspection=True)
    if argv:
        while argv and argv[0] in {
            "--no-pager",
            "--no-optional-locks",
            "--literal-pathspecs",
            "-C",
        }:
            argv = argv[2:] if argv[0] == "-C" else argv[1:]
    if not argv or argv[0] not in REMOTE_VERBS:
        raise ConfigError(
            "GitHub authentication requires one literal git clone/fetch/pull/push/ls-remote command, without shell chaining."
        )


@dataclass(frozen=True)
class GithubCredential:
    token: str = field(repr=False)

    @property
    def basic(self) -> str:
        return base64.b64encode(("x-access-token:" + self.token).encode("ascii")).decode("ascii")

    def configure(self, env: dict[str, str]) -> None:
        # Values stay in the helper's stdin and process environment: never argv,
        # repository config, global preferences, permission records or disk.
        config = [
            ("http.https://github.com/.extraHeader", ""),
            ("http.https://github.com/.extraHeader", "Authorization: Basic " + self.basic),
            ("http.followRedirects", "false"),
            ("http.sslVerify", "true"),
            ("url.https://github.com/.insteadOf", "git@github.com:"),
            ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
            ("url.https://github.com/.insteadOf", "ssh://git@ssh.github.com:443/"),
        ]
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value

    def redact(self, value: str) -> str:
        for secret in (self.token, self.basic):
            value = value.replace(secret, "[redacted GitHub credential]")
        return value


def load_credential() -> GithubCredential:
    from .windows_sandbox import _local_path
    from .windows_toolchain import known_folder, program_files

    parent = program_files()
    executable = parent / "GitHub CLI/gh.exe" if parent else None
    if executable is None or not executable.is_file():
        raise ConfigError(
            "GitHub authentication needs GitHub CLI installed in Program Files and an existing gh auth login for github.com."
        )
    executable = _local_path(executable, directory=False)
    # Do not execute a workspace-selected helper or inherit gh token/config/host
    # overrides. Read the existing standard user login, with no interactive UI.
    names = {
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    # Desktop test profiles relocate APPDATA. Authentication still belongs to
    # the OS user's standard GitHub CLI login, not that isolated app profile.
    roaming = known_folder(0x1A)
    if roaming is None:
        raise ConfigError("Cannot locate the user's standard GitHub CLI configuration.")
    env = {key: value for key, value in env.items() if key.upper() != "APPDATA"}
    env["APPDATA"] = str(roaming)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        result = subprocess.run(
            [str(executable), "auth", "token", "--hostname", "github.com"],
            cwd=executable.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **command_text(),
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError(
            "Could not read the existing GitHub CLI login. Run gh auth login outside the sandbox, then retry this operation."
        ) from None
    token = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9_]{20,256}", token):
        raise ConfigError(
            "No usable saved github.com login. Run gh auth login outside the sandbox, then retry this operation."
        )
    return GithubCredential(token)
