from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.hooks import (
    build_hook_audit_event,
    hook_audit_artifact_path,
    load_resolved_hooks_config,
    project_hooks_config_path,
)
from alysis_code.hooks.models import HookInvocationContext


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_hooks_trust_and_untrust_cli_updates_project_config_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": "echo session-start"}],
                    }
                ]
            }
        },
    )
    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}

    trust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env=env,
    )
    assert trust_result.exit_code == 0
    assert "Trusted project hooks config" in trust_result.output

    trusted_resolved = load_resolved_hooks_config(workspace)
    assert trusted_resolved.has_any_hooks is True
    assert trusted_resolved.untrusted_project_paths == ()

    untrust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "untrust", "--path", str(workspace)],
        env=env,
    )
    assert untrust_result.exit_code == 0
    assert "Untrusted project hooks config" in untrust_result.output

    untrusted_resolved = load_resolved_hooks_config(workspace)
    assert untrusted_resolved.has_any_hooks is False
    assert untrusted_resolved.untrusted_project_paths == (project_hooks_config_path(workspace),)


def test_hooks_trust_rejects_invalid_project_config(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": "", "id": "bad hook"}],
                    }
                ]
            }
        },
    )
    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env=env,
    )

    assert result.exit_code == 1
    assert "Invalid hooks config" in result.output


def test_hooks_doctor_reports_untrusted_project_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [{"type": "command", "command": "echo pre"}],
                    }
                ]
            }
        },
    )

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "doctor", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
        terminal_width=200,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "project" in normalized_output
    assert "untrusted" in normalized_output
    assert "hooks.json" in result.output


def test_hooks_list_shows_runtime_and_session_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo session-start",
                                "id": "session.bootstrap",
                                "runtimeKinds": ["interactive_chat"],
                                "sessionSource": ["startup"],
                            }
                        ]
                    }
                ]
            }
        },
    )

    trust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )
    assert trust_result.exit_code == 0

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "list", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
        terminal_width=220,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "session.bootstrap" in normalized_output
    assert "SessionStart" in normalized_output
    assert "interactive_chat" in normalized_output
    assert "startup" in normalized_output


def test_hooks_trace_reads_hook_audit_artifact(tmp_path: Path) -> None:
    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"
    session_id = "sess_1"
    artifact_path = hook_audit_artifact_path(sessions_dir=sessions_dir, session_id=session_id)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_event = build_hook_audit_event(
        session_id=session_id,
        context=HookInvocationContext(
            event_name="TurnComplete",
            source_path="/tmp/workspace/.alysis/hooks.json",
            source_scope="project",
            matcher="",
            hook_id="turn.stop",
            priority=0,
            failure_policy="warn",
            command="echo stop",
            timeout_s=5.0,
            trusted=True,
            returncode=0,
            blocked=False,
            modified_input=False,
            modified_input_fields=(),
            modified_prompt=False,
            modified_prompt_chars=0,
            additional_system_message_count=0,
            additional_user_message_count=0,
            stdout_chars=0,
            stderr_chars=0,
            duration_ms=12,
            status="ok",
            warnings=(),
        ),
    )
    artifact_path.write_text(json.dumps(artifact_event) + "\n", encoding="utf-8")

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "trace", session_id],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(cfg_dir),
            "ALYSIS_DATA_DIR": os.fspath(data_dir),
        },
        terminal_width=200,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "TurnComplete" in normalized_output
    assert "turn.stop" in normalized_output
    assert artifact_path.as_posix() in normalized_output


def test_hooks_test_reports_matching_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo start",
                                "id": "session.bootstrap",
                                "runtimeKinds": ["interactive_chat"],
                                "sessionSource": ["startup"],
                            }
                        ]
                    }
                ]
            }
        },
    )
    trust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )
    assert trust_result.exit_code == 0

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "test",
            "--path",
            str(workspace),
            "--event",
            "SessionStart",
            "--runtime-kind",
            "interactive_chat",
            "--session-source",
            "startup",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
        terminal_width=220,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "session.bootstrap" in normalized_output
    assert "matched" in normalized_output
    assert "yes" in normalized_output


def test_hooks_test_reports_runtime_kind_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo start",
                                "id": "session.bootstrap",
                                "runtimeKinds": ["interactive_chat"],
                                "sessionSource": ["startup"],
                            }
                        ]
                    }
                ]
            }
        },
    )
    trust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )
    assert trust_result.exit_code == 0

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "test",
            "--path",
            str(workspace),
            "--event",
            "SessionStart",
            "--runtime-kind",
            "subagent",
            "--session-source",
            "startup",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
        terminal_width=220,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "session.bootstrap" in normalized_output
    assert "runtime_kindmismatch" in normalized_output
    assert "no" in normalized_output


def test_hooks_test_reports_missing_tool_target_for_tool_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [{"type": "command", "command": "echo pre", "id": "pre.shell"}],
                    }
                ]
            }
        },
    )
    trust_result = runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )
    assert trust_result.exit_code == 0

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "test",
            "--path",
            str(workspace),
            "--event",
            "PreToolUse",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
        terminal_width=220,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "pre.shell" in normalized_output
    assert "missingtooltarget" in normalized_output
    assert "no" in normalized_output


def test_hooks_test_rejects_invalid_session_source(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "test",
            "--path",
            str(workspace),
            "--event",
            "SessionStart",
            "--session-source",
            "bad-source",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )

    assert result.exit_code == 2
    assert "session_source must be one of: startup, resume, fork" in result.output


def test_hooks_init_creates_starter_config_and_gitignore_entry(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("# existing\n", encoding="utf-8")

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "init", "--path", str(workspace)],
    )

    assert result.exit_code == 0
    local_config = workspace / ".alysis" / "hooks.local.json"
    assert local_config.exists()
    payload = json.loads(local_config.read_text(encoding="utf-8"))
    assert "hooks" in payload
    assert "SessionStart" in payload["hooks"]
    gitignore_text = (workspace / ".gitignore").read_text(encoding="utf-8")
    assert ".alysis/hooks.local.json" in gitignore_text


def test_hooks_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    local_config = workspace / ".alysis" / "hooks.local.json"
    local_config.parent.mkdir(parents=True, exist_ok=True)
    local_config.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "init", "--path", str(workspace)],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_hooks_effective_reports_per_hook_fire_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [
                            {
                                "type": "command",
                                "id": "pre.shell",
                                "command": "echo",
                                "runtimeKinds": ["interactive_chat"],
                            }
                        ],
                    }
                ]
            }
        },
    )
    runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "effective",
            "--path",
            str(workspace),
            "--event",
            "PreToolUse",
            "--tool",
            "shell_run",
            "--runtime",
            "interactive_chat",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )

    assert result.exit_code == 0
    normalized = "".join(result.output.split())
    assert "pre.shell" in normalized
    assert "yes" in normalized


def test_hooks_effective_flags_runtime_filter_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell_run",
                        "hooks": [
                            {
                                "type": "command",
                                "id": "pre.shell",
                                "command": "echo",
                                "runtimeKinds": ["one_shot"],
                            }
                        ],
                    }
                ]
            }
        },
    )
    runner.invoke(
        cli_mod.app,
        ["hooks", "trust", "--path", str(workspace)],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )

    result = runner.invoke(
        cli_mod.app,
        [
            "hooks",
            "effective",
            "--path",
            str(workspace),
            "--event",
            "PreToolUse",
            "--tool",
            "shell_run",
            "--runtime",
            "interactive_chat",
        ],
        env={"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)},
    )

    assert result.exit_code == 0
    normalized = "".join(result.output.split())
    assert "runtime_kind" in normalized
    assert "notin" in normalized


def test_hooks_enable_and_disable_toggle_local_layer(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    local_config = workspace / ".alysis" / "hooks.local.json"
    local_config.parent.mkdir(parents=True, exist_ok=True)
    local_config.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "id": "toggle.me",
                                    "command": "echo hi",
                                    "enabled": True,
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    disable_result = runner.invoke(
        cli_mod.app,
        ["hooks", "disable", "toggle.me", "--path", str(workspace)],
    )
    assert disable_result.exit_code == 0
    payload = json.loads(local_config.read_text(encoding="utf-8"))
    assert payload["hooks"]["SessionStart"][0]["hooks"][0]["enabled"] is False

    enable_result = runner.invoke(
        cli_mod.app,
        ["hooks", "enable", "toggle.me", "--path", str(workspace)],
    )
    assert enable_result.exit_code == 0
    payload = json.loads(local_config.read_text(encoding="utf-8"))
    assert payload["hooks"]["SessionStart"][0]["hooks"][0]["enabled"] is True


def test_hooks_enable_errors_on_unknown_id(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    local_config = workspace / ".alysis" / "hooks.local.json"
    local_config.parent.mkdir(parents=True, exist_ok=True)
    local_config.write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "enable", "no.such.hook", "--path", str(workspace)],
    )

    assert result.exit_code == 1
    assert "not found" in result.output


def _write_owned_session_log(
    sessions_dir: Path,
    *,
    session_id: str,
    owner: str | None,
    ts: str,
) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"owner": owner} if owner else {}
    events = [
        {
            "type": "session_start",
            "ts": ts,
            "session_id": session_id,
            **stamp,
            "payload": {"mode": "auto"},
        },
    ]
    (sessions_dir / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _write_minimal_hook_audit_artifact(
    sessions_dir: Path,
    *,
    session_id: str,
    hook_id: str,
) -> None:
    artifact_path = hook_audit_artifact_path(sessions_dir=sessions_dir, session_id=session_id)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_event = build_hook_audit_event(
        session_id=session_id,
        context=HookInvocationContext(
            event_name="TurnComplete",
            source_path="/tmp/workspace/.alysis/hooks.json",
            source_scope="project",
            matcher="",
            hook_id=hook_id,
            priority=0,
            failure_policy="warn",
            command="echo stop",
            timeout_s=5.0,
            trusted=True,
            returncode=0,
            blocked=False,
            modified_input=False,
            modified_input_fields=(),
            modified_prompt=False,
            modified_prompt_chars=0,
            additional_system_message_count=0,
            additional_user_message_count=0,
            stdout_chars=0,
            stderr_chars=0,
            duration_ms=12,
            status="ok",
            warnings=(),
        ),
    )
    artifact_path.write_text(json.dumps(artifact_event) + "\n", encoding="utf-8")


def test_hooks_trace_latest_default_skips_foreign_owner_sessions(tmp_path: Path) -> None:
    import uuid

    from alysis_code.session_store import local_session_owner

    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"

    # Foreign session is NEWEST by the log's own timestamp; without the owner
    # filter the latest-session default would read its artifact.
    foreign_id = "sess_foreign"
    _write_owned_session_log(
        sessions_dir,
        session_id=foreign_id,
        owner=f"foreign-user@foreign-host-{uuid.uuid4().hex}",
        ts="2026-07-11T12:00:00+00:00",
    )
    _write_minimal_hook_audit_artifact(
        sessions_dir, session_id=foreign_id, hook_id="foreign.hook.id"
    )

    own_id = "sess_own"
    _write_owned_session_log(
        sessions_dir,
        session_id=own_id,
        owner=local_session_owner(),
        ts="2026-07-10T12:00:00+00:00",
    )
    _write_minimal_hook_audit_artifact(sessions_dir, session_id=own_id, hook_id="own.hook.id")

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "trace"],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(cfg_dir),
            "ALYSIS_DATA_DIR": os.fspath(data_dir),
        },
        terminal_width=200,
    )

    normalized_output = "".join(result.output.split())
    assert result.exit_code == 0
    assert "own.hook.id" in normalized_output
    assert "foreign.hook.id" not in normalized_output


def test_hooks_trace_latest_default_explains_hidden_foreign_sessions(tmp_path: Path) -> None:
    import uuid

    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"

    _write_owned_session_log(
        sessions_dir,
        session_id="sess_foreign",
        owner=f"foreign-user@foreign-host-{uuid.uuid4().hex}",
        ts="2026-07-11T12:00:00+00:00",
    )

    result = runner.invoke(
        cli_mod.app,
        ["hooks", "trace"],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(cfg_dir),
            "ALYSIS_DATA_DIR": os.fspath(data_dir),
        },
        terminal_width=200,
    )

    assert result.exit_code == 0
    normalized_output = " ".join(result.output.split())
    assert "No sessions owned by this account" in normalized_output
    assert "different account" in normalized_output
