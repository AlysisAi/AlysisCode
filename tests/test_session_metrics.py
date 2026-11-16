from __future__ import annotations

import json
from pathlib import Path

from alysis_code.session_metrics import score_session_events, score_session_log


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    payload = "\n".join(json.dumps(event) for event in events) + "\n"
    path.write_text(payload, encoding="utf-8")


def test_score_session_events_collects_core_metrics() -> None:
    events = [
        {
            "type": "session_start",
            "session_id": "sid-1",
            "payload": {"system_prompt_sha256": "abc123"},
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read_lines",
                "step": 1,
                "arguments": {"path": "calc.py", "start_line": 10, "end_line": 20},
            },
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_write", "step": 2, "arguments": {"path": "calc.py"}},
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_write",
                "step": 2,
                "result": {"error": "Blocked write to protected path: .alysis/state.txt"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "step": 3,
                "arguments": {"cmd": "python3 -m unittest -v"},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "shell_run",
                "step": 3,
                "result": {"error": "same failure"},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "shell_run",
                "step": 4,
                "result": {"error": "same failure"},
            },
        },
        {
            "type": "llm_usage",
            "payload": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
                "cost_usd": 0.001,
            },
        },
    ]

    score = score_session_events(events)

    assert score["session_id"] == "sid-1"
    assert score["has_system_prompt_sha256"] is True
    assert score["tool_calls"] == 3
    assert score["write_calls"] == 1
    assert score["read_before_first_write"] is True
    assert score["tool_counts"]["fs_read_lines"] == 1
    assert score["tool_errors"] == 3
    assert score["blocked_write_errors"] == 1
    assert score["shell_runs"] == 1
    assert score["test_shell_runs"] == 1
    assert score["test_shell_commands"] == ["python3 -m unittest -v"]
    assert score["llm_usage_events"] == 1
    assert score["total_tokens"] == 125
    repeated = score["repeated_tool_errors"]
    assert isinstance(repeated, list)
    assert repeated and repeated[0]["count"] == 2


def test_score_session_events_tracks_unmetered_cost_separately() -> None:
    events = [
        {
            "type": "llm_usage",
            "payload": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost_usd": 0.02,
            },
        },
        {
            "type": "llm_usage",
            "payload": {
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "total_tokens": 60,
                "cost_usd": None,
            },
        },
    ]

    score = score_session_events(events)

    assert score["llm_usage_events"] == 2
    assert score["total_tokens"] == 180
    # The unmetered call must not be silently counted as $0.00.
    assert round(score["cost_usd"], 6) == 0.02
    assert score["known_cost_calls"] == 1
    assert score["unknown_cost_calls"] == 1


def test_score_session_log_defaults_session_id_to_filename(tmp_path: Path) -> None:
    path = tmp_path / "demo_session.jsonl"
    events = [
        {"type": "session_start", "payload": {}},
        {"type": "final", "payload": {"content": "ok"}},
    ]
    _write_jsonl(path, events)

    score = score_session_log(path)
    assert score["session_id"] == "demo_session"
    assert score["path"] == str(path)


def test_score_session_events_counts_verify_runs() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "verify_run",
                "step": 1,
                "arguments": {"commands": ["pytest -q", "ruff check ."]},
            },
        }
    ]

    score = score_session_events(events)

    assert score["verify_runs"] == 1
    assert score["tool_counts"]["verify_run"] == 1


def test_score_session_events_tracks_verification_selection_and_failure_kinds() -> None:
    events = [
        {
            "type": "session_start",
            "session_id": "sid-verify",
            "payload": {
                "verification_selection_source": "repo_scan.likely_test_commands",
                "verification_selection_reason": (
                    "repo scan discovered authoritative repo-native verification commands"
                ),
                "verification_contract_type": "repo_native",
                "verification_authoritative": True,
            },
        },
        {
            "type": "verify_run",
            "payload": {
                "all_passed": False,
                "verification_authoritative": True,
            },
        },
        {
            "type": "verification_contract_updated",
            "payload": {
                "verification_selection_source": "task_refinement.no_authoritative_commands",
                "verification_selection_reason": (
                    "docs-only task does not expose a confident verification command"
                ),
                "verification_contract_type": "unavailable",
                "verification_authoritative": False,
            },
        },
        {
            "type": "verify_run",
            "payload": {
                "all_passed": False,
                "verification_authoritative": False,
            },
        },
    ]

    score = score_session_events(events)

    assert score["session_id"] == "sid-verify"
    assert score["verification_selection_source"] == "task_refinement.no_authoritative_commands"
    assert (
        score["verification_selection_reason"]
        == "docs-only task does not expose a confident verification command"
    )
    assert score["verification_contract_type"] == "unavailable"
    assert score["verification_authoritative"] is False
    assert score["authoritative_verification_failures"] == 1
    assert score["non_authoritative_verification_failures"] == 1
    assert score["last_verification_failure_kind"] == "non_authoritative"


def test_score_session_events_tracks_web_metrics_and_duplicates() -> None:
    events = [
        {
            "type": "user_message",
            "payload": {"content": "Please inspect https://docs.example.com/start"},
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "web_search",
                "step": 1,
                "arguments": {"query": "docs example", "external_web_access": True},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs example",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Start",
                            "url": "https://docs.example.com/start",
                            "snippet": "alpha",
                        },
                        {
                            "title": "Guide",
                            "url": "https://docs.example.com/guide",
                            "snippet": "beta",
                        },
                    ],
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "web_search",
                "step": 2,
                "arguments": {"query": "docs   example", "external_web_access": True},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "web_search",
                "step": 2,
                "result": {
                    "query": "docs example",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Start",
                            "url": "https://docs.example.com/start",
                            "snippet": "alpha",
                        }
                    ],
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "web_fetch",
                "step": 3,
                "arguments": {"url": "https://docs.example.com/start"},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "web_fetch",
                "step": 3,
                "result": {
                    "url": "https://docs.example.com/start",
                    "final_url": "https://docs.example.com/final",
                    "status_code": 200,
                    "content_type": "text/html",
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "web_fetch",
                "step": 4,
                "arguments": {"url": "https://docs.example.com/start"},
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "web_fetch",
                "step": 4,
                "result": {
                    "url": "https://docs.example.com/start",
                    "final_url": "https://docs.example.com/final",
                    "status_code": 200,
                    "content_type": "text/html",
                },
            },
        },
    ]

    score = score_session_events(events)

    assert score["web_search_calls"] == 2
    assert score["web_fetch_calls"] == 2
    assert score["unique_web_queries"] == 1
    assert score["unique_web_fetch_urls"] == 1
    assert score["duplicate_web_queries"] == 1
    assert score["duplicate_web_fetches"] == 1
    assert score["total_web_sources_returned"] == 3
    assert score["total_web_sources_fetched"] == 2


def test_score_session_events_counts_fs_edit_as_write() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_edit",
                "step": 2,
                "arguments": {"path": "calc.py", "edits": [{"op": "replace_exact"}]},
            },
        }
    ]

    score = score_session_events(events)

    assert score["write_calls"] == 1
    assert score["tool_counts"]["fs_edit"] == 1


def test_score_session_events_counts_file_ops_as_writes() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_move",
                "step": 1,
                "arguments": {"source_path": "a.txt", "destination_path": "b.txt"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_copy",
                "step": 2,
                "arguments": {"source_path": "b.txt", "destination_path": "c.txt"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_delete",
                "step": 3,
                "arguments": {"path": "c.txt"},
            },
        },
    ]

    score = score_session_events(events)

    assert score["write_calls"] == 3
    assert score["tool_counts"]["fs_move"] == 1
    assert score["tool_counts"]["fs_copy"] == 1
    assert score["tool_counts"]["fs_delete"] == 1


def test_score_session_events_classifies_read_tools_before_a_write() -> None:
    cases = (
        ("git_history", {"mode": "log", "path": "src/app.py", "limit": 5}, "src/app.py"),
        ("symbol_search", {"query": "build_tools", "kind": "function"}, "x.py"),
        ("history_search", {"pattern": "verify", "include_history": True}, "x.py"),
    )
    for name, arguments, path in cases:
        events = [
            {"type": "tool_call", "payload": {"name": name, "step": 1, "arguments": arguments}},
            {
                "type": "tool_call",
                "payload": {"name": "fs_write", "step": 2, "arguments": {"path": path}},
            },
        ]
        score = score_session_events(events)
        assert score["tool_counts"][name] == 1, name
        assert score["read_before_first_write"] is True, name


def test_score_session_events_classifies_custom_tool_capabilities() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "jira_lookup",
                "step": 1,
                "arguments": {"issue_key": "ABC-1"},
                "tool_type": "custom_tool",
                "custom_tool": {
                    "manifest_version": 1,
                    "source_scope": "project",
                    "relative_tool_path": ".alysis/tools/jira_lookup.py",
                    "capabilities": {
                        "read_only": True,
                        "destructive": False,
                        "network_access": "restricted",
                        "filesystem_read_scope": "none",
                        "filesystem_write_scope": "none",
                        "secret_refs": ["JIRA_TOKEN"],
                    },
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "cleanup_workspace",
                "step": 2,
                "arguments": {},
                "tool_type": "custom_tool",
                "custom_tool": {
                    "manifest_version": 1,
                    "source_scope": "project",
                    "relative_tool_path": ".alysis/tools/cleanup.py",
                    "capabilities": {
                        "read_only": False,
                        "destructive": True,
                        "network_access": "none",
                        "filesystem_read_scope": "workspace",
                        "filesystem_write_scope": "workspace",
                        "secret_refs": [],
                    },
                },
            },
        },
    ]

    score = score_session_events(events)

    assert score["custom_tool_calls"] == 2
    assert score["custom_tool_counts"] == {
        "cleanup_workspace": 1,
        "jira_lookup": 1,
    }
    assert score["custom_tool_risk_counts"] == {"destructive": 1, "network": 1}
    assert score["tool_category_counts"]["custom_tool"] == 2
    assert score["tool_category_counts"]["network"] == 1
    assert score["tool_category_counts"]["destructive"] == 1
    assert score["write_calls"] == 1
    assert score["read_before_first_write"] is True


def test_score_session_events_custom_read_before_write_respects_step_order() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "reader",
                "step": 1,
                "arguments": {},
                "tool_type": "custom_tool",
                "custom_tool": {
                    "capabilities": {
                        "read_only": True,
                        "destructive": False,
                        "network_access": "none",
                        "filesystem_read_scope": "workspace",
                        "filesystem_write_scope": "none",
                        "secret_refs": [],
                    },
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "writer",
                "step": 1,
                "arguments": {},
                "tool_type": "custom_tool",
                "custom_tool": {
                    "capabilities": {
                        "read_only": False,
                        "destructive": False,
                        "network_access": "none",
                        "filesystem_read_scope": "none",
                        "filesystem_write_scope": "workspace",
                        "secret_refs": [],
                    },
                },
            },
        },
    ]

    score = score_session_events(events)

    assert score["read_before_first_write"] is False
