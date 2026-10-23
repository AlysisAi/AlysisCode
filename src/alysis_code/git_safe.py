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


def build_git_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    source.update(_NON_INTERACTIVE_GIT_ENV)
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
