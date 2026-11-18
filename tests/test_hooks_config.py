from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.hooks import (
    load_hook_config_file,
    load_resolved_hooks_config,
    project_hooks_config_path,
    project_local_hooks_config_path,
    trust_project_hooks_config,
    user_hooks_config_path,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_load_resolved_hooks_config_merges_user_project_and_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        user_hooks_config_path(),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": "echo user-start"}],
                    }
                ]
            }
        },
    )
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [{"type": "command", "command": "echo project-pre"}],
                    }
                ]
            }
        },
    )
    _write_json(
        project_local_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "fs_write",
                        "hooks": [{"type": "command", "command": "echo local-pre"}],
                    }
                ]
            }
        },
    )
    trust_project_hooks_config(
        workspace_root=workspace,
        config_path=project_hooks_config_path(workspace),
    )

    resolved = load_resolved_hooks_config(workspace)

    assert resolved.has_any_hooks is True
    assert resolved.loaded_paths == (
        user_hooks_config_path(),
        project_hooks_config_path(workspace),
        project_local_hooks_config_path(workspace),
    )
    session_start_groups = resolved.groups_for_event("SessionStart")
    assert len(session_start_groups) == 1
    assert session_start_groups[0].hooks[0].command == "echo user-start"
    pre_tool_groups = resolved.groups_for_event("PreToolUse")
    assert [group.matcher for group in pre_tool_groups] == ["shell_run", "fs_write"]
    assert [group.hooks[0].command for group in pre_tool_groups] == [
        "echo project-pre",
        "echo local-pre",
    ]
    assert resolved.untrusted_project_paths == ()


def test_load_resolved_hooks_config_rejects_invalid_hook_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [{"type": "command", "command": ""}],
                    }
                ]
            }
        },
    )
    trust_project_hooks_config(
        workspace_root=workspace,
        config_path=project_hooks_config_path(workspace),
    )

    with pytest.raises(ConfigError, match="Invalid hooks config"):
        load_resolved_hooks_config(workspace)


def test_load_resolved_hooks_config_skips_untrusted_project_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [{"type": "command", "command": "echo project-pre"}],
                    }
                ]
            }
        },
    )

    resolved = load_resolved_hooks_config(workspace)

    assert resolved.has_any_hooks is False
    assert resolved.loaded_paths == (project_hooks_config_path(workspace),)
    assert resolved.untrusted_project_paths == (project_hooks_config_path(workspace),)
    assert resolved.groups_for_event("PreToolUse") == ()


def test_load_resolved_hooks_config_accepts_v11_hook_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        user_hooks_config_path(),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo user-start",
                                "id": "session.bootstrap",
                                "description": "Bootstrap session context",
                                "priority": -5,
                                "failurePolicy": "continue",
                                "runtimeKinds": ["interactive_chat"],
                                "sessionSource": ["startup"],
                                "timeoutMs": 2500,
                            }
                        ],
                    }
                ]
            }
        },
    )

    resolved = load_resolved_hooks_config(workspace)

    hook = resolved.groups_for_event("SessionStart")[0].hooks[0]
    assert hook.id == "session.bootstrap"
    assert hook.description == "Bootstrap session context"
    assert hook.priority == -5
    assert hook.failure_policy == "continue"
    assert hook.runtime_kinds == ["interactive_chat"]
    assert hook.session_source == ["startup"]
    assert hook.timeout == 2.5


def test_hook_runtime_kinds_keep_retired_runtimes_inert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    def write_hooks(*hooks: dict[str, object]) -> None:
        _write_json(
            user_hooks_config_path(),
            {"hooks": {"SessionStart": [{"hooks": list(hooks)}]}},
        )

    write_hooks(
        {
            "type": "command",
            "command": "echo retired",
            "id": "retired.only",
            "runtimeKinds": ["swarm_worker"],
        },
        {
            "type": "command",
            "command": "echo mixed",
            "id": "mixed.kinds",
            "runtimeKinds": ["Interactive_Chat", "forge_exec"],
        },
    )

    hooks = {
        hook.id: hook
        for hook in load_resolved_hooks_config(workspace).groups_for_event("SessionStart")[0].hooks
    }

    # A hook scoped only to a runtime that no longer exists keeps that scope, so it matches
    # no live runtime instead of widening to all of them or failing the whole file.
    assert hooks["retired.only"].runtime_kinds == ["swarm_worker"]
    assert hooks["mixed.kinds"].runtime_kinds == ["interactive_chat", "forge_exec"]

    write_hooks(
        {
            "type": "command",
            "command": "echo typo",
            "id": "typo.kind",
            "runtimeKinds": ["interactive-chat"],
        }
    )
    with pytest.raises(ConfigError):
        load_resolved_hooks_config(workspace)


def test_load_resolved_hooks_config_overrides_same_hook_id_with_more_specific_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        user_hooks_config_path(),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo user-guard",
                                "id": "policy.guard",
                            }
                        ],
                    }
                ]
            }
        },
    )
    _write_json(
        project_local_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "fs_write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo local-guard",
                                "id": "policy.guard",
                            }
                        ],
                    }
                ]
            }
        },
    )

    resolved = load_resolved_hooks_config(workspace)

    pre_tool_groups = resolved.groups_for_event("PreToolUse")
    assert len(pre_tool_groups) == 1
    assert pre_tool_groups[0].matcher == "fs_write"
    assert pre_tool_groups[0].source_scope == "project_local"
    assert [hook.command for hook in pre_tool_groups[0].hooks] == ["echo local-guard"]


def test_load_resolved_hooks_config_rejects_invalid_hook_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        user_hooks_config_path(),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo user-start",
                                "id": "Bad ID",
                            }
                        ],
                    }
                ]
            }
        },
    )

    with pytest.raises(ConfigError, match="Invalid hooks config"):
        load_resolved_hooks_config(workspace)


def test_config_load_propagates_unicode_error(tmp_path: Path) -> None:
    """A hooks config file with invalid UTF-8 bytes raises ConfigError."""
    config_path = tmp_path / "hooks.json"
    config_path.write_bytes(b"\xff\xfe\x00invalid json bytes")
    with pytest.raises(ConfigError, match="Failed to read hooks config"):
        load_hook_config_file(config_path)


def test_load_resolved_hooks_config_rejects_duplicate_hook_ids_within_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    _write_json(
        user_hooks_config_path(),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo first",
                                "id": "dup.hook",
                            },
                            {
                                "type": "command",
                                "command": "echo second",
                                "id": "dup.hook",
                            },
                        ],
                    }
                ]
            }
        },
    )

    with pytest.raises(ConfigError, match="Invalid hooks config"):
        load_resolved_hooks_config(workspace)
