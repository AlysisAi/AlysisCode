from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.mcp.prompts import (
    McpPromptMessage,
    normalize_get_prompt_result,
    normalize_list_prompts_result,
)
from alysis_code.mcp.untrusted_content import build_untrusted_mcp_text_block

_FIXTURE_SERVER = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "minimal_stdio_server.py"
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_user_stdio_prompt_config(
    tmp_path: Path,
    *,
    fixture_payload: dict,
    prompts_mode: str = "listed_get_only",
    enabled_in: list[str] | None = None,
) -> dict[str, str]:
    cfg_dir = tmp_path / "cfg"
    fixture_path = tmp_path / "fixture-server.json"
    _write_json(fixture_path, fixture_payload)
    server_payload: dict[str, object] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [os.fspath(_FIXTURE_SERVER)],
        "env": {
            "ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_path),
        },
        "prompts_mode": prompts_mode,
    }
    if enabled_in is not None:
        server_payload["enabled_in"] = list(enabled_in)
    _write_json(
        cfg_dir / "mcp.json",
        {
            "servers": {
                "alpha": server_payload,
            }
        },
    )
    return {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}


def _write_user_multi_server_prompt_config(
    tmp_path: Path,
    *,
    servers: dict[str, dict[str, object]],
) -> dict[str, str]:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    config_servers: dict[str, dict[str, object]] = {}
    for server_id, spec in servers.items():
        fixture_payload = spec.get("fixture_payload")
        payload: dict[str, object] = {
            "transport": "stdio",
            "command": spec.get("command", sys.executable),
            "prompts_mode": spec.get("prompts_mode", "listed_get_only"),
        }
        if fixture_payload is not None:
            fixture_path = tmp_path / f"{server_id}-fixture-server.json"
            _write_json(fixture_path, fixture_payload)
            payload["args"] = [os.fspath(_FIXTURE_SERVER)]
            payload["env"] = {
                "ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_path),
            }
        elif "args" in spec:
            payload["args"] = list(spec["args"])
        enabled_in = spec.get("enabled_in")
        if enabled_in is not None:
            payload["enabled_in"] = list(enabled_in)
        config_servers[server_id] = payload
    _write_json(cfg_dir / "mcp.json", {"servers": config_servers})
    return {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}


def test_normalize_list_prompts_result_is_deterministic() -> None:
    prompts, next_cursor = normalize_list_prompts_result(
        {
            "prompts": [
                {
                    "name": "review_pr",
                    "title": "Review Pull Request",
                    "description": "Prepare a review prompt.",
                    "arguments": [
                        {
                            "name": "repo",
                            "description": "Repository slug",
                            "required": True,
                        },
                        {
                            "name": "number",
                            "required": False,
                        },
                    ],
                }
            ],
            "nextCursor": "page:1",
        }
    )

    assert next_cursor == "page:1"
    assert [prompt.name for prompt in prompts] == ["review_pr"]
    assert prompts[0].title == "Review Pull Request"
    assert prompts[0].description == "Prepare a review prompt."
    assert [argument.as_payload() for argument in prompts[0].arguments] == [
        {"name": "repo", "description": "Repository slug", "required": True},
        {"name": "number", "required": False},
    ]


# The summary asserts a literal character count, so the fixture and the
# expectation have to move together. Naming it once stops them drifting apart —
# the rename shortened this string from 20 chars to 17 and broke the assertion.
_JSON_FIXTURE = '{"repo":"alysis"}'


def test_normalize_get_prompt_result_builds_safe_readable_summary() -> None:
    normalized = normalize_get_prompt_result(
        {
            "name": "review_pr",
            "description": "Review helper",
            "messages": [
                {
                    "role": "system",
                    "content": {
                        "type": "text",
                        "text": "Review PR 123 carefully.",
                    },
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "mimeType": "application/json",
                            "text": _JSON_FIXTURE,
                        },
                        {
                            "type": "image",
                            "mimeType": "image/png",
                            "uri": "https://example.com/pr.png",
                        },
                    ],
                },
            ],
        },
        expected_name="review_pr",
    )

    assert normalized.description == "Review helper"
    assert normalized.text == f"Review PR 123 carefully.\n\n{_JSON_FIXTURE}"
    assert "system: text(24 chars)" in normalized.content_summary
    assert (
        f"user: application/json text({len(_JSON_FIXTURE)} chars), image/png image item omitted"
        in (normalized.content_summary)
    )
    assert normalized.messages[1].content[1]["summary"] == "image/png image item omitted"


def test_mcp_prompt_message_payload_wraps_untrusted_text_with_provenance() -> None:
    message = McpPromptMessage(
        role="user",
        content=(
            {
                "type": "text",
                "mime_type": "application/json",
                "summary": "application/json text(4 chars)",
                "text": "body",
            },
        ),
        content_summary="application/json text(4 chars)",
        text="body",
    )

    payload = message.as_payload(server_id="alpha", prompt_name="review_pr")

    expected_text = build_untrusted_mcp_text_block(
        source_type="prompt_get",
        server_id="alpha",
        source_name="review_pr",
        text="body",
    )
    expected_content_text = build_untrusted_mcp_text_block(
        source_type="prompt_get",
        server_id="alpha",
        source_name="review_pr",
        text="body",
        mime_type="application/json",
    )
    assert payload["text"] == expected_text
    assert payload["content"][0]["text"] == expected_content_text


def test_mcp_prompts_list_cli_lists_manual_prompts_deterministically(tmp_path: Path) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [
                [
                    {
                        "name": "review_pr",
                        "title": "Review Pull Request",
                        "description": "Review a pull request.",
                        "arguments": [{"name": "repo", "required": True}],
                    },
                    {
                        "name": "draft_issue",
                        "description": "Draft an issue body.",
                    },
                ]
            ],
        },
    )

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "prompts", "list", "--path", str(tmp_path), "--limit", "5"],
        env=env,
        terminal_width=200,
    )
    normalized_output = "".join(result.output.split())

    assert result.exit_code == 0
    assert "MCPPrompts(2)" in normalized_output
    assert "review_pr" in normalized_output
    assert "ReviewPullRequest" in normalized_output
    assert "repo*" in normalized_output
    assert "draft_issue" in normalized_output


def test_mcp_prompts_get_cli_supports_arg_parsing_and_json_output(tmp_path: Path) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [
                [
                    {
                        "name": "review_pr",
                        "title": "Review Pull Request",
                        "arguments": [{"name": "repo", "required": True}],
                    }
                ]
            ],
            "prompt_get_results": {
                "review_pr": {
                    "name": "review_pr",
                    "description": "Review helper",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": "Review repo owner/alysis.",
                            },
                        }
                    ],
                }
            },
        },
    )

    result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "get",
            "alpha",
            "review_pr",
            "--path",
            str(tmp_path),
            "--arg",
            "repo=owner/alysis",
            "--json",
        ],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["server_id"] == "alpha"
    assert payload["name"] == "review_pr"
    assert payload["title"] == "Review Pull Request"
    assert payload["applied_arguments"] == {"repo": "owner/alysis"}
    assert payload["messages"][0]["role"] == "user"
    expected_text = build_untrusted_mcp_text_block(
        source_type="prompt_get",
        server_id="alpha",
        source_name="review_pr",
        text="Review repo owner/alysis.",
    )
    assert payload["text"] == expected_text
    assert payload["messages"][0]["text"] == expected_text
    assert payload["messages"][0]["content"][0]["text"] == expected_text


def test_mcp_prompts_get_cli_human_output_renders_wrapped_text_literally(tmp_path: Path) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [[{"name": "review_pr", "title": "Review Pull Request"}]],
            "prompt_get_results": {
                "review_pr": {
                    "name": "review_pr",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": "Review repo owner/alysis.",
                            },
                        }
                    ],
                }
            },
        },
    )

    result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "get",
            "alpha",
            "review_pr",
            "--path",
            str(tmp_path),
        ],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert "MarkupError" not in result.output
    assert "[MCP_UNTRUSTED_TEXT]" in result.output
    assert "[/MCP_UNTRUSTED_TEXT]" in result.output
    assert "Review repo owner/alysis." in result.output


def test_mcp_prompt_cli_refresh_flags_are_host_controlled_and_deterministic(
    tmp_path: Path,
) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [[{"name": "review_pr", "title": "Review Pull Request"}]],
            "prompt_get_results": {
                "review_pr": {
                    "name": "review_pr",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": "fresh prompt body",
                            },
                        }
                    ],
                }
            },
        },
    )

    list_result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "list",
            "--path",
            str(tmp_path),
            "--limit",
            "5",
            "--refresh",
            "--json",
        ],
        env=env,
        terminal_width=200,
    )
    get_result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "get",
            "alpha",
            "review_pr",
            "--path",
            str(tmp_path),
            "--refresh",
            "--json",
        ],
        env=env,
        terminal_width=200,
    )

    assert list_result.exit_code == 0
    list_payload = json.loads(list_result.output)
    assert list_payload["refresh_performed"] is True
    assert [prompt["name"] for prompt in list_payload["prompts"]] == ["review_pr"]

    assert get_result.exit_code == 0
    get_payload = json.loads(get_result.output)
    assert get_payload["refresh_performed"] is True
    assert get_payload["server_id"] == "alpha"
    assert get_payload["name"] == "review_pr"
    expected_text = build_untrusted_mcp_text_block(
        source_type="prompt_get",
        server_id="alpha",
        source_name="review_pr",
        text="fresh prompt body",
    )
    assert get_payload["text"] == expected_text
    assert get_payload["messages"][0]["text"] == expected_text
    assert get_payload["messages"][0]["content"][0]["text"] == expected_text


def test_mcp_status_cli_reports_snapshot_and_stale_observability(tmp_path: Path) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [[{"name": "review_pr"}]],
        },
    )

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "status", "--path", str(tmp_path), "--json"],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["bootstrap_errors"] == []
    assert payload["prompt_errors"] == {}
    assert payload["rows"] == [
        {
            "server_id": "alpha",
            "transport": "stdio",
            "tools_snapshot_loaded": True,
            "tools_snapshot_count": 0,
            "tools_snapshot_stale": False,
            "resources_snapshot_loaded": False,
            "resources_snapshot_count": None,
            "resources_snapshot_stale": False,
            "prompts_enabled": True,
            "prompts_snapshot_loaded": True,
            "prompts_snapshot_count": 1,
            "prompts_snapshot_stale": False,
            "manual_prompt_refresh_available": True,
            "operator_action": [],
            "prompt_error": None,
            "startup_error": None,
            # No desktop rule in this environment: the permission-mode rules apply.
            "call_approval": "permission_mode",
            "tools": [],
        }
    ]
    assert payload["tool_server_approval"] == {"version": 1, "active": False}


def test_mcp_prompts_list_cli_resolves_project_root_from_nested_path(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", os.fspath(tmp_path)], check=True)
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [
                [
                    {
                        "name": "review_pr",
                        "title": "Review Pull Request",
                    }
                ]
            ],
        },
    )
    _write_json(
        tmp_path / ".alysis" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "prompts_mode": "disabled",
                }
            }
        },
    )
    nested = tmp_path / "pkg" / "subdir"
    nested.mkdir(parents=True)

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "prompts", "list", "--path", str(nested), "--limit", "5"],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert "No MCP prompts available." in result.output


def test_mcp_prompts_list_cli_supports_explicit_runtime_selection(tmp_path: Path) -> None:
    env = _write_user_stdio_prompt_config(
        tmp_path,
        fixture_payload={
            "capabilities": {"prompts": {}},
            "prompts_pages": [
                [
                    {
                        "name": "review_pr",
                    }
                ]
            ],
        },
        enabled_in=["forge_exec"],
    )

    default_result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "prompts", "list", "--path", str(tmp_path), "--limit", "5"],
        env=env,
        terminal_width=200,
    )
    explicit_runtime_result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "list",
            "--path",
            str(tmp_path),
            "--runtime",
            "forge_exec",
            "--limit",
            "5",
        ],
        env=env,
        terminal_width=200,
    )

    assert default_result.exit_code == 0
    assert "No MCP prompts available." in default_result.output
    assert explicit_runtime_result.exit_code == 0
    assert "review_pr" in explicit_runtime_result.output


def test_targeted_mcp_prompt_cli_operations_skip_unrelated_broken_servers(tmp_path: Path) -> None:
    broken_command = os.fspath(tmp_path / "missing-beta-mcp-server")
    env = _write_user_multi_server_prompt_config(
        tmp_path,
        servers={
            "alpha": {
                "fixture_payload": {
                    "capabilities": {"prompts": {}},
                    "prompts_pages": [
                        [
                            {
                                "name": "ok_prompt",
                                "title": "OK Prompt",
                            }
                        ]
                    ],
                    "prompt_get_results": {
                        "ok_prompt": {
                            "name": "ok_prompt",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": {
                                        "type": "text",
                                        "text": "ok prompt body",
                                    },
                                }
                            ],
                        }
                    },
                },
            },
            "beta": {
                "command": broken_command,
            },
        },
    )

    targeted_list = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "list",
            "--path",
            str(tmp_path),
            "--server",
            "alpha",
            "--limit",
            "5",
        ],
        env=env,
        terminal_width=200,
    )
    targeted_get = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "get",
            "alpha",
            "ok_prompt",
            "--path",
            str(tmp_path),
            "--json",
        ],
        env=env,
        terminal_width=200,
    )
    global_list = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "list",
            "--path",
            str(tmp_path),
            "--limit",
            "5",
        ],
        env=env,
        terminal_width=200,
    )

    assert targeted_list.exit_code == 0
    assert "ok_prompt" in targeted_list.output
    assert targeted_get.exit_code == 0
    targeted_get_payload = json.loads(targeted_get.output)
    assert targeted_get_payload["server_id"] == "alpha"
    assert targeted_get_payload["name"] == "ok_prompt"
    expected_text = build_untrusted_mcp_text_block(
        source_type="prompt_get",
        server_id="alpha",
        source_name="ok_prompt",
        text="ok prompt body",
    )
    assert targeted_get_payload["text"] == expected_text
    assert targeted_get_payload["messages"][0]["text"] == expected_text
    assert targeted_get_payload["messages"][0]["content"][0]["text"] == expected_text
    assert global_list.exit_code == 1
    assert broken_command in "".join(global_list.output.split())


def test_mcp_prompts_list_cli_rejects_blank_server_filter_without_bootstrap(
    tmp_path: Path,
) -> None:
    request_log = tmp_path / "alpha-prompt-requests.jsonl"
    broken_command = os.fspath(tmp_path / "missing-beta-mcp-server")
    env = _write_user_multi_server_prompt_config(
        tmp_path,
        servers={
            "alpha": {
                "fixture_payload": {
                    "capabilities": {"prompts": {}},
                    "prompts_pages": [[{"name": "ok_prompt"}]],
                    "record_client_requests_path": os.fspath(request_log),
                },
            },
            "beta": {
                "command": broken_command,
            },
        },
    )

    result = CliRunner().invoke(
        cli_mod.app,
        [
            "mcp",
            "prompts",
            "list",
            "--path",
            str(tmp_path),
            "--server",
            "   ",
            "--limit",
            "5",
        ],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 1
    assert "server_id must be a non-empty string when present." in result.output
    assert not request_log.exists()


def test_mcp_status_cli_reports_a_failed_server_on_its_own_row(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    fixture_path = tmp_path / "fixture-server.json"
    _write_json(
        fixture_path,
        {"tools_pages": [[{"name": "echo", "inputSchema": {"type": "object"}}]]},
    )
    _write_json(
        cfg_dir / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [os.fspath(_FIXTURE_SERVER)],
                    "env": {"ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_path)},
                },
                "broken": {
                    "transport": "stdio",
                    "command": "alysis-test-command-that-does-not-exist",
                },
            }
        },
    )
    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "status", "--path", str(tmp_path), "--json"],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bootstrap_errors"] == []
    rows = {row["server_id"]: row for row in payload["rows"]}
    assert rows["alpha"]["tools_snapshot_loaded"] is True
    assert rows["alpha"]["tools_snapshot_count"] == 1
    assert rows["alpha"]["startup_error"] is None
    assert rows["broken"]["tools_snapshot_loaded"] is False
    assert "failed to launch process" in rows["broken"]["startup_error"]
    assert payload["metadata"]["failed_server_ids"] == ["broken"]

    table = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "status", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )
    assert table.exit_code == 0, table.output
    assert "failed" in table.output
    assert "MCP server broken did not start:" in table.output
