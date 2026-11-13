from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .branding import env_get_from

_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
_NON_INTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "GIT_EDITOR": "true",
    "GIT_MERGE_AUTOEDIT": "no",
    "PAGER": "cat",
}


def separate_git_directories(
    root: Path, *, name_rule: bool = True, enclosing: bool = False
) -> tuple[Path, ...]:
    """Git directories that ``root``'s ``.git`` file points to outside any ``.git`` folder.

    ``git init --separate-git-dir`` and some worktree layouts leave a ``.git``
    file in the folder and keep the repository somewhere else. Protecting the
    name ``.git`` does not cover that directory, yet Git run on ``root`` takes
    its configuration and hooks from there. The pointer is read as text; Git
    is not run. A directory already under a ``.git`` folder is left out,
    because a caller that protects the name ``.git`` wherever it appears
    already covers it. A caller that protects exact paths only has no such
    rule and passes ``name_rule=False`` to be given those directories too.

    A pointer that names ``root`` itself or a folder containing it is left out
    as well: it cannot be write-protected without stopping every write. A
    caller that needs to know such a pointer exists passes ``enclosing=True``.
    """

    def head(path: Path) -> str:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(4096)

    pointer = root / ".git"
    try:
        if pointer.is_symlink() or not pointer.is_file():
            return ()
        first = (head(pointer).splitlines() or [""])[0]
        value = first[len("gitdir:") :].strip() if first.startswith("gitdir:") else ""
        if not value:
            return ()
        workspace = root.resolve()
        gitdir = (root / value).resolve()
        candidates = [gitdir]
        common = gitdir / "commondir"
        try:
            shared = head(common).strip() if common.is_file() else ""
        except OSError:
            if os.name != "nt":
                raise
            # On Windows a Git command that is running keeps this name from
            # being created: it is listed and cannot be opened. The Git
            # directory itself is still one to report.
            shared = ""
        if shared:
            candidates.append((gitdir / shared).resolve())
        return tuple(
            dict.fromkeys(
                candidate
                for candidate in candidates
                if candidate.is_dir()
                and not (name_rule and any(part.casefold() == ".git" for part in candidate.parts))
                # Not the workspace itself or a folder containing it, unless
                # asked: protecting that would stop every write.
                and (enclosing or not workspace.is_relative_to(candidate))
            )
        )
    except (OSError, RuntimeError, ValueError):
        return ()


def git_hooks_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(env_get_from(source, "ALYSIS_GIT_HOOKS") or "").strip().lower()
    return raw in _TRUE_VALUES


def resolve_disabled_hooks_dir(root: Path, env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    override = str(env_get_from(source, "ALYSIS_GIT_HOOKS_PATH") or "").strip()
    if override:
        target = Path(override).expanduser()
    else:
        root_hash = hashlib.sha256(os.fspath(root.resolve()).encode("utf-8")).hexdigest()[:16]
        target = Path(tempfile.gettempdir()) / "alysis-agent" / "hooks-disabled" / root_hash
    target.mkdir(parents=True, exist_ok=True)
    return target


# Host-side Git inspects folders the agent can write, outside any sandbox, so it
# must not run what such a folder supplies. A directory that merely contains
# HEAD, objects and refs is a repository only when named explicitly, and a
# repository's filesystem-monitor program is never started.
_HOST_GIT_CONFIG = (("safe.bareRepository", "explicit"), ("core.fsmonitor", "false"))


def build_git_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    source.update(_NON_INTERACTIVE_GIT_ENV)
    try:
        count = max(0, int(source.get("GIT_CONFIG_COUNT") or 0))
    except ValueError:
        count = 0
    # The last entry for a key wins, so append unless ours already is the last.
    effective = {
        source.get(f"GIT_CONFIG_KEY_{index}", "").lower(): source.get(f"GIT_CONFIG_VALUE_{index}")
        for index in range(count)
    }
    for key, value in _HOST_GIT_CONFIG:
        if effective.get(key.lower()) != value:
            source[f"GIT_CONFIG_KEY_{count}"] = key
            source[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
    source["GIT_CONFIG_COUNT"] = str(count)
    return source


def build_git_cmd(
    root: Path,
    args: list[str],
    *,
    extra_config: dict[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    disable_filters: bool = False,
) -> list[str]:
    config = dict(extra_config or {})
    if not git_hooks_enabled(env):
        config.setdefault("core.hooksPath", os.fspath(resolve_disabled_hooks_dir(root, env)))

    if disable_filters:
        config.update(_disabled_filter_config(root, config=config, env=env))

    cmd: list[str] = ["git", "-C", os.fspath(root)]
    for key, value in config.items():
        cmd.extend(["-c", f"{key}={value}"])
    cmd.extend(args)
    return cmd


def _disabled_filter_config(
    root: Path, *, config: dict[str, str], env: Mapping[str, str] | None
) -> dict[str, str]:
    """Refuse external conversions without silently substituting unfiltered bytes.

    Ask Git for its effective configuration, including includes, worktree settings
    and environment overrides. Git itself resolves attributes when it uses the
    returned command, so unused drivers do not prevent ordinary repositories from
    working. Re-read for every command: a child can introduce filter configuration.
    """
    drivers = configured_git_filter_drivers(root, extra_config=config, env=env)
    overrides: dict[str, str] = {}
    for driver in drivers:
        # Empty all three commands, including the long-running process protocol.
        # required=true makes an attempted conversion fail instead of passing
        # through bytes that might differ from the real filter's representation.
        overrides.update({f"filter.{driver}.{kind}": "" for kind in ("clean", "smudge", "process")})
        overrides[f"filter.{driver}.required"] = "true"
    return overrides


def configured_git_filter_drivers(
    root: Path,
    *,
    extra_config: dict[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Read effective nonempty filter commands without invoking any driver."""
    result = subprocess.run(
        build_git_cmd(
            root,
            ["config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
            extra_config=extra_config,
            env=env,
        ),
        env=build_git_process_env(env),
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode == 1:  # No configured filter commands.
        return ()
    if result.returncode != 0:
        raise OSError("Could not inspect Git filter configuration")
    values: dict[str, str] = {}
    for entry in result.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if entry:
            key, separator, value = entry.partition("\n")
            if not separator:
                raise OSError("Could not read Git filter configuration")
            values[key] = value
    return tuple(
        sorted({key[len("filter.") :].rsplit(".", 1)[0] for key, value in values.items() if value})
    )
