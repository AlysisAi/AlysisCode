from __future__ import annotations

from pathlib import Path

import pytest
from test_host_actions import _build_tools
from test_ide_host_actions import (
    _make_bridge,
    _respond,
    _response_for,
    _start_host_action,
    _wait_for,
)

from alysis_code.desktop_connections import CONNECTION_TOOLS
from alysis_code.host_actions import (
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
)

STATUS = {
    "connector": "github",
    "plugin": "enabled",
    "connection": "connected",
    "account": "selected-user",
    "evidence": "authenticated_identity",
    "checked_at": "2026-10-07T16:00:00.000Z",
    "next_step": "none",
}
ACTIONS = tuple(spec[0] for spec in CONNECTION_TOOLS)


@pytest.mark.parametrize("mode", ["auto", "readonly", "review"])
def test_connection_tools_are_read_only_negotiated_and_project_independent(
    tmp_path: Path, mode: str
):
    calls = []
    tools = _build_tools(
        tmp_path,
        mode=mode,
        actions=ACTIONS,
        handler=lambda action, args: calls.append((action, args)) or STATUS,
    )
    assert {"connection_status", "github_repositories"} <= tools.keys()
    assert tools["connection_status"].run({"connector": "github"}) == STATUS
    assert calls == [("connectors.status", {"connector": "github"})]
    absent = _build_tools(tmp_path, mode=mode, handler=lambda *_: STATUS)
    assert not {"connection_status", "github_repositories"} & absent.keys()
    # A normal directory, without .git, is sufficient for account questions.
    assert not (tmp_path / ".git").exists()


@pytest.mark.parametrize(
    "action,args",
    [
        ("connectors.status", {"connector": ["github"]}),
        ("connectors.status", {"connector": "github", "account": "other"}),
        ("connectors.status", {"connector": "unknown"}),
        ("connectors.github.repositories", {"page": 0}),
        ("connectors.github.repositories", {"page": True}),
        ("connectors.github.repositories", {"page": 10001}),
        ("connectors.github.repositories", {"owner": "other"}),
    ],
)
def test_model_cannot_expand_connection_authority(action, args):
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(action, args)


@pytest.mark.parametrize(
    "change",
    [
        {"token": "never"},
        {"connection": "connected", "account": None},
        {"connection": "connected", "evidence": "none"},
        {"plugin": []},
        {"connection": []},
        {"next_step": []},
        {"evidence": []},
        {"reason": []},
        {"connection": "unknown", "account": "selected-user"},
        {"evidence": "saved_grant"},
    ],
)
def test_malformed_connection_evidence_is_rejected(change):
    with pytest.raises(HostActionError):
        normalize_host_action_result("connectors.status", {**STATUS, **change})


def test_bounded_repository_metadata_rejects_credentials_or_foreign_urls():
    result = {
        "account": "selected-user",
        "repositories": [
            {"name": "org/private", "url": "https://github.com/org/private", "private": True}
        ],
        "next_page": 2,
    }
    assert normalize_host_action_result("connectors.github.repositories", result) == result
    for change in [{"token": "never"}, {"url": "https://elsewhere.test/org/private"}]:
        changed = {**result, "repositories": [{**result["repositories"][0], **change}]}
        with pytest.raises(HostActionError):
            normalize_host_action_result("connectors.github.repositories", changed)
    with pytest.raises(HostActionError):
        normalize_host_action_result(
            "connectors.github.repositories",
            {**result, "repositories": result["repositories"] * 101},
        )


def test_production_bridge_round_trip_and_untrusted_workspace(tmp_path: Path, monkeypatch):
    out, bridge, session_id, captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=ACTIONS,
        action="connectors.status",
        arguments={"connector": "github"},
    )
    try:
        assert captured["host_action_capabilities"] == frozenset(ACTIONS)
        event = _start_host_action(out, bridge, session_id)
        _respond(bridge, event, request_id="status", result=STATUS)
        assert _response_for(out, "status")["ok"] is True
        _wait_for(out, lambda row: row.get("type") == "message_end")
        assert results == [STATUS]
        assert errors == []
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "prompt,tool_name,arguments,result",
    [
        ("hi", None, {}, None),
        ("Show the diff in my local repository", "git_diff", {}, None),
        ("Do you have access to my GitHub?", "connection_status", {"connector": "github"}, STATUS),
        (
            "have you access to my github?",
            "connection_status",
            {"connector": "github"},
            {
                **STATUS,
                "connection": "disconnected",
                "account": None,
                "evidence": "none",
                "next_step": "link-account",
            },
        ),
        (
            "Do you have access to my GitHub?",
            "connection_status",
            {"connector": "github"},
            {
                **STATUS,
                "connection": "unknown",
                "account": None,
                "evidence": "none",
                "next_step": "refresh_plugin",
            },
        ),
        (
            "Can you see my repositories?",
            "github_repositories",
            {},
            {
                "account": "selected-user",
                "repositories": [
                    {
                        "name": "org/private",
                        "url": "https://github.com/org/private",
                        "private": True,
                    }
                ],
                "next_page": None,
            },
        ),
        (
            "Are you connected to Google Drive?",
            "connection_status",
            {"connector": "google_drive"},
            {
                **STATUS,
                "connector": "google_drive",
                "account": "selected@example.test",
                "evidence": "saved_grant",
            },
        ),
    ],
)
def test_natural_messages_use_normal_agent_loop_with_scripted_provider(
    tmp_path: Path, prompt, tool_name, arguments, result
):
    """Controlled model selection and connector evidence, not live provider acceptance."""
    import json
    import subprocess

    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall
    from alysis_code.surface import NoopSurface

    calls = []
    records = []
    if tool_name == "git_diff":

        def git(*args):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

        git("init", "-b", "main")
        (tmp_path / "example.txt").write_text("before\n")
        git("add", "example.txt")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "Fixture",
        )
        (tmp_path / "example.txt").write_text("after\n")

    class ScriptedClient:
        model = "gpt-4o-mini"
        temperature = 0.2

        def chat(self, *, messages, tools=None, **kwargs):
            records.append(messages)
            if len(records) == 1:
                assert any(
                    prompt in str(message.get("content", ""))
                    for message in messages
                    if message["role"] == "user"
                )
                names = {entry["function"]["name"] for entry in tools or []}
                assert {"connection_status", "github_repositories"} <= names
                if tool_name:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(id="connection-check", name=tool_name, arguments=arguments)
                        ],
                        raw={},
                    )
            else:
                assert len(records) == 2
                outputs = [message for message in messages if message.get("role") == "tool"]
                assert outputs
                if tool_name == "git_diff":
                    assert (
                        "-before" in outputs[-1]["content"] and "+after" in outputs[-1]["content"]
                    )
                else:
                    payload = json.loads(outputs[-1]["content"])
                    assert payload == result
            return LLMResponse(content="Controlled model response.", tool_calls=[], raw={})

    def host(action, args):
        calls.append((action, args))
        return normalize_host_action_result(action, result)

    session = create_session(
        cfg=AppConfig(model="gpt-4o-mini", stream=False),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=6,
        no_log=True,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        session_log_dir_override=tmp_path / "sessions",
        host_action_capabilities=ACTIONS,
        host_action_handler=host,
    )
    session.client = ScriptedClient()
    try:
        assert session.run_turn(prompt) == 0
        assert len(records) == (2 if tool_name else 1)
        assert len(calls) == (1 if tool_name and tool_name != "git_diff" else 0)
        if tool_name and tool_name != "git_diff":
            assert calls == [
                (next(spec[0] for spec in CONNECTION_TOOLS if spec[1] == tool_name), arguments)
            ]
    finally:
        session.close()


def test_untrusted_workspace_does_not_gain_account_tools(tmp_path: Path, monkeypatch):
    _, bridge, _, captured, _, _ = _make_bridge(
        tmp_path, monkeypatch, workspace_trusted=False, actions=ACTIONS
    )
    try:
        assert not captured["host_action_capabilities"]
    finally:
        bridge.close()
