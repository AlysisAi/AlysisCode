from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.mcp.config import (
    load_resolved_mcp_config,
    redact_sensitive_mapping,
    user_mcp_config_path,
)
from alysis_code.mcp.errors import McpConfigError
from alysis_code.runtime_kind import RuntimeKind


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_stdio_server(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "transport": "stdio",
        "command": "tool",
    }
    payload.update(overrides)
    return payload


def _base_http_server(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "transport": "http",
        "url": "https://example.com/mcp",
    }
    payload.update(overrides)
    return payload


def test_mcp_config_rejects_unknown_user_server_fields_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": "tool",
                    "unexpected": True,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "unexpected" in message


def test_mcp_config_rejects_invalid_enabled_in_runtime_kind_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": "tool",
                    "enabled_in": ["bogus"],
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "enabled_in" in message
    assert "runtime kind" in message


def test_user_mcp_config_accepts_schema_version_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "schema_version": 1,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    assert [server.id for server in resolved.servers] == ["alpha"]


def test_project_mcp_config_accepts_schema_version_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "schema_version": 1,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "schema_version": 1,
            "servers": {
                "alpha": {
                    "enabled": False,
                }
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.enabled is False


def test_user_mcp_config_accepts_roots_mode_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(roots_mode="workspace"),
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.roots_mode == "workspace"


def test_project_mcp_config_may_narrow_roots_mode_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(roots_mode="workspace"),
            },
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "roots_mode": "disabled",
                }
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.roots_mode == "disabled"


def test_project_mcp_config_cannot_broaden_roots_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(roots_mode="disabled"),
            },
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "roots_mode": "workspace",
                }
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "roots_mode" in message
    assert "broaden roots exposure" in message


def test_user_mcp_config_accepts_resources_mode_listed_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(resources_mode="listed_read_only"),
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.resources_mode == "listed_read_only"


def test_project_mcp_config_may_narrow_resources_mode_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(resources_mode="listed_read_only"),
            },
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "resources_mode": "disabled",
                }
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.resources_mode == "disabled"


def test_project_mcp_config_cannot_broaden_resources_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(resources_mode="disabled"),
            },
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "resources_mode": "listed_read_only",
                }
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "resources_mode" in message
    assert "broaden resources exposure" in message


def test_user_mcp_config_accepts_prompts_mode_listed_get_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(prompts_mode="listed_get_only"),
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.prompts_mode == "listed_get_only"


def test_project_mcp_config_may_narrow_prompts_mode_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(prompts_mode="listed_get_only"),
            },
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "prompts_mode": "disabled",
                }
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.prompts_mode == "disabled"


def test_project_mcp_config_cannot_broaden_prompts_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(prompts_mode="disabled"),
            },
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "prompts_mode": "listed_get_only",
                }
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "prompts_mode" in message
    assert "broaden prompts exposure" in message


@pytest.mark.parametrize("schema_version", [2, 999])
def test_user_mcp_config_rejects_unsupported_schema_version_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "schema_version": schema_version,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "schema_version" in message
    assert "1" in message


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_user_mcp_config_rejects_non_integer_schema_version_lookalikes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: object
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "schema_version": schema_version,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "schema_version" in message
    assert "valid integer" in message or "integer" in message


@pytest.mark.parametrize("schema_version", [2, 999])
def test_project_mcp_config_rejects_unsupported_schema_version_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "schema_version": 1,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "schema_version": schema_version,
            "servers": {
                "alpha": {
                    "enabled": False,
                }
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "schema_version" in message
    assert "1" in message


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_project_mcp_config_rejects_non_integer_schema_version_lookalikes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: object
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "schema_version": 1,
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "schema_version": schema_version,
            "servers": {
                "alpha": {
                    "enabled": False,
                }
            },
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "schema_version" in message
    assert "valid integer" in message or "integer" in message


def test_mcp_config_accepts_omitted_schema_version_for_backward_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(),
            },
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "enabled": False,
                }
            },
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.enabled is False


def test_user_mcp_config_reports_user_path_for_mixed_case_allow_deny_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_stdio_server(
                    allowed_tools=["fs_read"],
                    denied_tools=["FS_READ"],
                ),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert os.fspath(tmp_path / ".alysis" / "mcp.json") not in message
    assert "alpha" in message
    assert "allowed_tools/denied_tools" in message
    assert "cannot overlap after merge" in message


def test_project_mcp_config_cannot_reenable_user_disabled_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(enabled=False),
            }
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "enabled": True,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert "enabled" in message
    assert "cannot re-enable" in message


def test_project_mcp_config_can_disable_user_enabled_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(enabled=True),
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "enabled": False,
                }
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.enabled is False


def test_project_mcp_config_can_narrow_enabled_in_from_all_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(),
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "enabled_in": ["one_shot", "subagent"],
                }
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert [kind.value for kind in server.enabled_in or ()] == ["one_shot", "subagent"]


def test_mcp_enabled_in_skips_retired_runtimes_without_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "mixed": _base_stdio_server(enabled_in=["interactive_chat", "forge_exec"]),
                "retired": _base_stdio_server(enabled_in=["swarm_worker"]),
            }
        },
    )

    servers = {
        server.id: server for server in load_resolved_mcp_config(workspace_root=tmp_path).servers
    }

    assert [kind.value for kind in servers["mixed"].enabled_in or ()] == ["interactive_chat"]
    # A server scoped only to runtimes that no longer exist runs nowhere; it is not widened.
    assert servers["retired"].enabled_in == ()
    assert not any(servers["retired"].enabled_for(kind) for kind in RuntimeKind)
    assert servers["retired"].redacted_connection_payload()["enabled_in"] == []


def test_project_mcp_config_rejects_broader_enabled_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(enabled_in=["interactive_chat"]),
            }
        },
    )
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "enabled_in": ["interactive_chat", "one_shot"],
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert "enabled_in" in message
    assert "cannot broaden enabled_in beyond user config" in message


def test_project_mcp_config_rejects_superset_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(allowed_tools=["fs_read"]),
            }
        },
    )
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "allowed_tools": ["fs_read", "verify_run"],
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert "allowed_tools" in message
    assert "cannot broaden allowed_tools beyond user config" in message


def test_project_mcp_config_accepts_allowlist_when_user_has_no_explicit_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(),
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "allowed_tools": ["verify_run", "fs_read"],
                }
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.allowed_tools == ("verify_run", "fs_read")


def test_project_mcp_config_unions_denied_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(denied_tools=["shell_run"]),
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "denied_tools": ["verify_run", "shell_run"],
                }
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.denied_tools == ("shell_run", "verify_run")


@pytest.mark.parametrize(
    ("field_name", "project_value"), [("startup_timeout_s", 16), ("call_timeout_s", 31)]
)
def test_project_mcp_config_cannot_increase_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    project_value: float,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(startup_timeout_s=15, call_timeout_s=30),
            }
        },
    )
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    field_name: project_value,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert field_name in message
    assert f"cannot increase {field_name}" in message


def test_project_mcp_config_can_reduce_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(startup_timeout_s=15, call_timeout_s=30),
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "startup_timeout_s": 10,
                    "call_timeout_s": 20,
                }
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.startup_timeout_s == 10.0
    assert server.call_timeout_s == 20.0


def test_project_mcp_config_rejects_merged_allow_deny_overlap_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(allowed_tools=["fs_read"]),
            }
        },
    )
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    "denied_tools": ["fs_read"],
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert "allowed_tools/denied_tools" in message
    assert "cannot overlap after merge" in message


def test_project_mcp_config_cannot_introduce_new_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": "tool",
                }
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "beta": {
                    "enabled": True,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert "unknown server id 'beta'" in message
    assert os.fspath(tmp_path / ".alysis" / "mcp.json") in message


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("transport", "http"),
        ("command", "other-tool"),
        ("args", ["--debug"]),
        ("env", {"TOKEN": "x"}),
        ("url", "https://example.com/mcp"),
        ("headers", {"Authorization": "Bearer x"}),
        ("oauth", {"client_id": "other-client"}),
    ],
)
def test_project_mcp_config_cannot_override_connection_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: object,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": "tool",
                }
            }
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    field_name: field_value,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert f"field '{field_name}'" in message
    assert "project config cannot override this field" in message
    assert "alpha" in message


@pytest.mark.parametrize(
    ("field_name", "field_value"), [("trust", "explicit"), ("tool_prefix", "corp")]
)
def test_project_mcp_config_cannot_override_non_monotonic_policy_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: object,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_stdio_server(),
            }
        },
    )
    project_path = tmp_path / ".alysis" / "mcp.json"
    _write_json(
        project_path,
        {
            "servers": {
                "alpha": {
                    field_name: field_value,
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(project_path) in message
    assert "alpha" in message
    assert f"field '{field_name}'" in message
    assert "project config cannot override this field" in message


def test_user_http_config_rejects_remote_plain_http_url_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_http_server(url="http://example.com/mcp"),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    assert isinstance(excinfo.value, McpConfigError)
    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "url" in message
    assert "https://" in message


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765/mcp",
        "http://127.0.0.1:8765/mcp",
        "http://[::1]:8765/mcp",
    ],
)
def test_user_http_config_allows_loopback_plain_http_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_http_server(url=url),
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.transport == "http"
    assert server.url == url


def test_user_mcp_config_rejects_unsupported_trust_mode_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_stdio_server(trust="internal"),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "trust" in message
    assert "explicit" in message


def test_user_http_oauth_config_is_resolved_and_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_http_server(
                    oauth={
                        "client_id": "public-client",
                        "redirect_host": "localhost",
                        "redirect_port": 8765,
                        "scopes": [" profile ", "email", "profile", "", "email"],
                        "authorization_server_url": "https://auth.example.com",
                    }
                ),
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.transport == "http"
    assert server.oauth is not None
    assert server.oauth.client_id == "public-client"
    assert server.oauth.redirect_host == "localhost"
    assert server.oauth.redirect_port == 8765
    assert server.oauth.scopes == ("profile", "email")
    assert server.oauth.authorization_server_url == "https://auth.example.com"


def test_static_header_only_http_config_remains_valid_without_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_http_server(
                    headers={
                        "Authorization": "Bearer static-token",
                        "X-Workspace": "main",
                    }
                ),
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.headers == {
        "Authorization": "Bearer static-token",
        "X-Workspace": "main",
    }
    assert server.oauth is None


def test_mcp_oauth_scope_normalization_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "alpha": _base_http_server(
                    oauth={
                        "client_id": "public-client",
                        "scopes": ["openid", " profile ", "openid", "profile", ""],
                    }
                ),
            }
        },
    )

    resolved = load_resolved_mcp_config(workspace_root=tmp_path)

    [server] = resolved.servers
    assert server.oauth is not None
    assert server.oauth.scopes == ("openid", "profile")


def test_mcp_config_rejects_oauth_with_static_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_http_server(
                    headers={"Authorization": "Bearer static-token", "X-Workspace": "main"},
                    oauth={"client_id": "public-client"},
                ),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "oauth" in message
    assert "Authorization" in message


def test_mcp_config_rejects_oauth_on_stdio_server_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_stdio_server(oauth={"client_id": "public-client"}),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "oauth" in message


def test_mcp_config_rejects_invalid_oauth_authorization_server_url_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_http_server(
                    oauth={
                        "client_id": "public-client",
                        "authorization_server_url": "http://auth.example.com",
                    }
                ),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "authorization_server_url" in message
    assert "https://" in message


def test_mcp_config_rejects_invalid_oauth_redirect_port_with_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_http_server(
                    oauth={
                        "client_id": "public-client",
                        "redirect_port": 70000,
                    }
                ),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "redirect_port" in message
    assert "65535" in message


def test_mcp_config_rejects_oauth_redirect_host_with_embedded_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "alpha": _base_http_server(
                    oauth={
                        "client_id": "public-client",
                        "redirect_host": "localhost:8765",
                    }
                ),
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path)

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "redirect_host" in message
    assert "without a port" in message


def test_mcp_env_expansion_and_redaction_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        user_mcp_config_path(),
        {
            "servers": {
                "stdio-one": {
                    "transport": "stdio",
                    "command": "tool",
                    "args": ["--token", "stdio-arg-secret"],
                    "env": {
                        "TOKEN": "${STDIO_TOKEN}",
                    },
                },
                "http-one": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer ${HTTP_TOKEN}",
                    },
                },
            }
        },
    )

    resolved = load_resolved_mcp_config(
        workspace_root=tmp_path,
        env={"STDIO_TOKEN": "stdio-secret", "HTTP_TOKEN": "http-secret"},
    )

    by_id = {server.id: server for server in resolved.servers}
    stdio_server = by_id["stdio-one"]
    http_server = by_id["http-one"]
    assert stdio_server.args == ("--token", "stdio-arg-secret")
    assert stdio_server.env == {"TOKEN": "stdio-secret"}
    assert http_server.headers == {"Authorization": "Bearer http-secret"}
    assert redact_sensitive_mapping(stdio_server.env) == {"TOKEN": "[redacted]"}
    assert redact_sensitive_mapping(http_server.headers) == {"Authorization": "[redacted]"}
    assert stdio_server.redacted_connection_payload()["args"] == ["[redacted]", "[redacted]"]
    assert stdio_server.redacted_connection_payload()["env"] == {"TOKEN": "[redacted]"}
    assert http_server.redacted_connection_payload()["headers"] == {"Authorization": "[redacted]"}
    assert stdio_server.redacted_connection_payload()["roots_mode"] == "disabled"
    assert stdio_server.redacted_connection_payload()["resources_mode"] == "disabled"
    assert stdio_server.redacted_connection_payload()["prompts_mode"] == "disabled"
    assert "stdio-arg-secret" not in repr(stdio_server)
    assert "stdio-secret" not in repr(stdio_server)
    assert "http-secret" not in repr(http_server)


def test_mcp_env_expansion_missing_variable_raises_clear_error_without_secret_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    config_path = user_mcp_config_path()
    _write_json(
        config_path,
        {
            "servers": {
                "http-one": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "prefix-${HTTP_TOKEN}-suffix",
                    },
                }
            }
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_resolved_mcp_config(workspace_root=tmp_path, env={})

    message = str(excinfo.value)
    assert os.fspath(config_path) in message
    assert "${HTTP_TOKEN}" in message
    assert "prefix-" not in message
