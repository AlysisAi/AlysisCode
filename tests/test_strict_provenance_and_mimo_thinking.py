"""Explicit thinking and streaming for scored runs, and the MiMo reasoning wire.

A run could have thinking on by provider default while the session recorded
``llm_enable_thinking=None``: the MiMo token-plan route resolved to an unknown
reasoning contract, so no thinking field was ever sent, even when a value was
configured, and ``reasoning_content`` from tool-call turns was never sent back
although MiMo's "Deep Thinking" guide requires it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_loop import create_session
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import (
    OpenAICompatClient,
    assistant_message_from_response,
)
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface

_MIMO_HOSTS = (
    "https://api.xiaomimimo.com/v1",
    "https://token-plan-ams.xiaomimimo.com/v1",
)
_LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {}},
    },
}


# --- --strict-provenance ------------------------------------------------------


def _invoke_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str):
    calls: list[dict[str, object]] = []

    def fake_run_agent(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "k",
            *extra,
            "hi",
        ],
        env=env,
    )
    return result, calls


@pytest.fixture
def _no_thinking_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("ALYSIS_LLM_ENABLE_THINKING", raising=False)


@pytest.mark.usefixtures("_no_thinking_env")
def test_strict_provenance_refuses_to_start_without_an_explicit_stream_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "true")

    result, calls = _invoke_run(tmp_path, monkeypatch, "--strict-provenance")

    assert result.exit_code == 2
    assert "pass --stream or --no-stream" in result.output
    assert calls == []


@pytest.mark.usefixtures("_no_thinking_env")
def test_strict_provenance_refuses_to_start_without_an_explicit_thinking_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls = _invoke_run(tmp_path, monkeypatch, "--strict-provenance", "--stream")

    assert result.exit_code == 2
    assert "ALYSIS_LLM_ENABLE_THINKING" in result.output
    assert "pass --stream or --no-stream" not in result.output
    assert calls == []


@pytest.mark.usefixtures("_no_thinking_env")
@pytest.mark.parametrize("stream_flag", ["--stream", "--no-stream"])
@pytest.mark.parametrize("thinking", ["true", "false"])
def test_strict_provenance_starts_when_stream_and_thinking_are_both_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_flag: str, thinking: str
) -> None:
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", thinking)

    result, calls = _invoke_run(tmp_path, monkeypatch, "--strict-provenance", stream_flag)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1


@pytest.mark.usefixtures("_no_thinking_env")
def test_without_strict_provenance_the_defaults_still_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls = _invoke_run(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1


# --- MiMo reasoning wire ------------------------------------------------------


def _recording_client(base_url: str, bodies: list[dict], responses: list[dict], **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=responses[min(len(bodies), len(responses)) - 1])

    return OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model="mimo-v2.5",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.parametrize("base_url", _MIMO_HOSTS)
@pytest.mark.parametrize(
    ("enable_thinking", "expected"),
    [
        pytest.param(True, {"type": "enabled"}, id="on"),
        pytest.param(False, {"type": "disabled"}, id="off"),
        pytest.param(None, None, id="unset"),
    ],
)
def test_mimo_hosts_send_the_documented_thinking_switch_when_it_is_set(
    base_url: str, enable_thinking: bool | None, expected: dict | None
) -> None:
    bodies: list[dict] = []
    client = _recording_client(
        base_url,
        bodies,
        [{"choices": [{"message": {"content": "ok"}}]}],
        enable_thinking=enable_thinking,
    )

    client.chat(messages=[{"role": "user", "content": "hi"}])

    assert client.reasoning_trace_adapter == "deepseek_reasoning"
    assert bodies[0].get("thinking") == expected
    assert "reasoning_effort" not in bodies[0]


def test_mimo_sends_back_reasoning_content_on_tool_call_turns_only() -> None:
    bodies: list[dict] = []
    client = _recording_client(
        "https://token-plan-ams.xiaomimimo.com/v1",
        bodies,
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "think-before-tool",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"message": {"content": "answer", "reasoning_content": "think-before-answer"}}
                ]
            },
            {"choices": [{"message": {"content": "ok"}}]},
        ],
        enable_thinking=True,
    )
    messages: list[dict] = [{"role": "user", "content": "go"}]

    first = client.chat(messages=messages, tools=[_LOOKUP_TOOL])
    messages.append(assistant_message_from_response(first))
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": "result"})
    second = client.chat(messages=messages, tools=[_LOOKUP_TOOL])
    messages.append(assistant_message_from_response(second))
    messages.append({"role": "user", "content": "next"})
    client.chat(messages=messages, tools=[_LOOKUP_TOOL])

    tool_turn, text_turn = [m for m in bodies[2]["messages"] if m["role"] == "assistant"]
    assert tool_turn["reasoning_content"] == "think-before-tool"
    assert "reasoning_content" not in text_turn


def test_mimo_with_thinking_on_still_sends_a_forced_tool_choice() -> None:
    bodies: list[dict] = []
    client = _recording_client(
        "https://token-plan-ams.xiaomimimo.com/v1",
        bodies,
        [{"choices": [{"message": {"content": "ok"}}]}],
        enable_thinking=True,
    )
    forced = {"type": "function", "function": {"name": "lookup"}}

    client.chat(
        messages=[{"role": "user", "content": "hi"}], tools=[_LOOKUP_TOOL], tool_choice=forced
    )

    assert bodies[0]["tool_choice"] == forced


def test_an_explicit_reasoning_adapter_is_kept_on_a_mimo_host() -> None:
    bodies: list[dict] = []
    client = _recording_client(
        "https://api.xiaomimimo.com/v1",
        bodies,
        [{"choices": [{"message": {"content": "ok"}}]}],
        enable_thinking=True,
        reasoning_trace_adapter="none",
    )

    client.chat(messages=[{"role": "user", "content": "hi"}])

    assert client.reasoning_trace_adapter == "none"
    assert "thinking" not in bodies[0]


def test_other_hosts_keep_the_automatic_adapter_and_send_no_guessed_field() -> None:
    bodies: list[dict] = []
    client = _recording_client(
        "https://example.com/v1",
        bodies,
        [{"choices": [{"message": {"content": "ok"}}]}],
        enable_thinking=True,
    )

    client.chat(messages=[{"role": "user", "content": "hi"}])

    assert client.reasoning_trace_adapter == "auto"
    assert "thinking" not in bodies[0]


# --- config snapshot ----------------------------------------------------------


def test_config_snapshot_records_stream_thinking_output_cap_host_and_subagent_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "false")
    cfg = AppConfig(model="mimo-v2.5", base_url="https://token-plan-ams.xiaomimimo.com/v1")
    cfg.stream = False

    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        snapshots = [
            event
            for event in read_session_events(session.store.path)
            if event.get("type") == "config_snapshot"
        ]
    finally:
        session.close()

    effective = snapshots[0]["payload"]["effective"]
    assert effective["stream"] is False
    assert effective["llm_enable_thinking"] is False
    assert effective["llm_enable_thinking_explicit"] is True
    assert effective["max_output_tokens"] is None
    assert effective["max_output_tokens_source"] == "not_sent"
    assert effective["base_url_host"] == "token-plan-ams.xiaomimimo.com"
    assert effective["reasoning_trace_adapter"] == "deepseek_reasoning"
    assert isinstance(effective["subagents_enabled"], bool)
    assert effective["subagent_parallel_nonwriting_shared"] is False
