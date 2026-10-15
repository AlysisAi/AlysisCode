from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.git_safe import (
    build_git_cmd,
    git_hooks_enabled,
    resolve_disabled_hooks_dir,
)


def test_git_hooks_enabled_truthy_values() -> None:
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "1"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "true"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "enable"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "enabled"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "off"}) is False
    assert git_hooks_enabled({}) is False


def test_git_settings_honor_canonical_environment_names(tmp_path: Path) -> None:
    source_env = {
        "ALYSIS_GIT_HOOKS": "enabled",
        "ALYSIS_GIT_HOOKS_PATH": str(tmp_path / "custom-hooks"),
    }

    assert git_hooks_enabled(source_env) is True
    assert resolve_disabled_hooks_dir(tmp_path, source_env) == tmp_path / "custom-hooks"


def test_resolve_disabled_hooks_dir_honors_override(tmp_path: Path) -> None:
    override = tmp_path / "hooks-custom"
    target = resolve_disabled_hooks_dir(tmp_path, {"ALYSIS_GIT_HOOKS_PATH": str(override)})
    assert target == override
    assert target.exists()
    assert target.is_dir()


def test_build_git_cmd_adds_hooks_path_by_default(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        env={"ALYSIS_GIT_HOOKS_PATH": str(hooks_dir)},
    )
    assert cmd[:3] == ["git", "-C", str(tmp_path)]
    assert "-c" in cmd
    assert f"core.hooksPath={hooks_dir}" in cmd


def test_build_git_cmd_omits_hooks_override_when_enabled(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        env={
            "ALYSIS_GIT_HOOKS": "enable",
            "ALYSIS_GIT_HOOKS_PATH": str(hooks_dir),
        },
    )
    assert all(not str(part).startswith("core.hooksPath=") for part in cmd)


def test_build_git_cmd_respects_explicit_core_hooks_path(tmp_path: Path) -> None:
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        extra_config={"core.hooksPath": "manual", "user.name": "alysis"},
        env={"ALYSIS_GIT_HOOKS_PATH": str(tmp_path / "ignored")},
    )
    assert "core.hooksPath=manual" in cmd
    assert sum(1 for part in cmd if str(part).startswith("core.hooksPath=")) == 1


@pytest.mark.parametrize("source", ["include", "global", "environment", "command"])
def test_filter_guard_uses_effective_config_sources(tmp_path: Path, source: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", os.fspath(root)], check=True)
    marker = tmp_path / "filter-executed"
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\nimport sys\nPath(sys.argv[1]).touch()",
            os.fspath(marker),
        ]
    )
    key = "filter.Mixed.Case-driver.clean"
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    extra_config: dict[str, str] = {}
    if source in {"include", "global"}:
        config_file = tmp_path / "external.config"
        subprocess.run(
            ["git", "config", "--file", os.fspath(config_file), key, command], check=True
        )
        if source == "global":
            env["GIT_CONFIG_GLOBAL"] = os.fspath(config_file)
        else:
            subprocess.run(
                ["git", "-C", os.fspath(root), "config", "include.path", os.fspath(config_file)],
                check=True,
            )
    elif source == "environment":
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=key, GIT_CONFIG_VALUE_0=command)
    else:
        extra_config[key] = command
    (root / ".gitattributes").write_text("* filter=Mixed.Case-driver\n", encoding="utf-8")
    (root / "input").write_bytes(b"original\x00bytes")
    args = ["hash-object", "--path=input", "input"]

    guarded = build_git_cmd(root, args, env=env, extra_config=extra_config, disable_filters=True)
    result = subprocess.run(guarded, env=env, capture_output=True)

    assert result.returncode != 0
    assert not marker.exists()
    # Control: this is an actual external command and Git would execute it under
    # the same configuration without the guard.
    subprocess.run(
        build_git_cmd(root, args, env=env, extra_config=extra_config), env=env, capture_output=True
    )
    assert marker.exists()


def test_filter_guard_respects_an_explicitly_disabled_command(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", os.fspath(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(tmp_path), "config", "filter.Example.clean", "false"], check=True
    )
    (tmp_path / ".gitattributes").write_text("* filter=Example\n", encoding="utf-8")
    (tmp_path / "input").write_text("original\n", encoding="utf-8")
    result = subprocess.run(
        build_git_cmd(
            tmp_path,
            ["hash-object", "--path=input", "input"],
            extra_config={"filter.Example.clean": ""},
            disable_filters=True,
        ),
        capture_output=True,
    )
    assert result.returncode == 0


def test_filter_guard_fails_closed_when_config_cannot_be_read(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", os.fspath(tmp_path)], check=True)
    with (tmp_path / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("\n[invalid-section\n")
    with pytest.raises(OSError, match="Could not inspect Git filter configuration"):
        build_git_cmd(tmp_path, ["add", "--all"], disable_filters=True)
