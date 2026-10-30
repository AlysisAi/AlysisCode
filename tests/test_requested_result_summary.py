from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from test_mcp_visual_delivery import config
from test_sensitive_tool_output_boundary import _RecordingHooks, _RecordingSurface

from alysis_code.agent.sensitive_output import (
    collect_requested_summary_protections,
    redact_sensitive_response_taints,
    requested_result_summary_text,
)
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.mcp.retention import ephemeral_tool_io, requested_result_summary
from alysis_code.runtime_kind import RuntimeKind

POLICY = {
    "ai.alysis/retention": {
        "version": 2,
        "mode": "ephemeral",
        "assistant_summary": "requested_results",
    }
}
NAME = "mcp__summary_fixture__inspect"
RAW = "PRIVATE_UNRELATED_Καλημέρα_91bc"
CREDENTIAL = "private-credential-93bc"
SNAPSHOT = "private-snapshot-0092"
SUMMARY = "Calculator refused the keyboard input. The requested result remains unverified."


def test_summary_policy_is_explicit_and_does_not_relax_v1():
    assert ephemeral_tool_io({"_meta": POLICY})
    assert requested_result_summary({"_meta": POLICY})
    assert not requested_result_summary(
        {"_meta": {"ai.alysis/retention": {"version": 1, "mode": "ephemeral"}}}
    )
    for value in (
        {"version": 2, "mode": "ephemeral"},
        {"version": 2, "mode": "ephemeral", "assistant_summary": "anything"},
        {"version": True, "mode": "ephemeral"},
    ):
        with pytest.raises(ValueError):
            ephemeral_tool_io({"_meta": {"ai.alysis/retention": value}})


def test_short_private_values_stay_private_and_redaction_does_not_rewrite_itself():
    response = LLMResponse(content="secret=0; x; key", tool_calls=[], raw={})
    safe = redact_sensitive_response_taints(response, {"0", "x", "key", "a"})
    assert (
        safe.content
        == "secret=[redacted: approved sensitive content]; [redacted: approved sensitive content]; [redacted: approved sensitive content]"
    )
    assert redact_sensitive_response_taints(safe, {"0", "x", "key", "a"}).content == safe.content


def test_summary_protects_credentials_and_capability_ids_without_numeric_label_taints():
    observation = {
        "elements": [{"label": str(n), "value": str(n)} for n in range(10)],
        "snapshot_id": SNAPSHOT,
        "fields": [{"label": "Password", "value": CREDENTIAL}, {"api_key": "s"}],
    }
    protected = collect_requested_summary_protections(
        {"content": [{"type": "text", "text": json.dumps(observation)}]}
    )
    assert {SNAPSHOT, CREDENTIAL, "s"}.issubset(protected)
    assert not set(map(str, range(10))).intersection(protected)
    response = LLMResponse(content=f"83,810,205; {CREDENTIAL}; {SNAPSHOT}", tool_calls=[], raw={})
    summary = requested_result_summary_text(response, protected)
    assert "83,810,205" in summary
    assert CREDENTIAL not in summary and SNAPSHOT not in summary
    assert (
        requested_result_summary_text(
            LLMResponse(
                content="result", tool_calls=[ToolCall(id="a", name=NAME, arguments={})], raw={}
            ),
            protected,
        )
        is None
    )
    blocked = requested_result_summary_text(
        LLMResponse(content=json.dumps(observation), tool_calls=[], raw={}), protected
    )
    assert SNAPSHOT not in blocked and CREDENTIAL not in blocked


@pytest.mark.parametrize("error", [False, True])
def test_requested_summary_preserves_raw_retention_and_public_tool_guard(
    tmp_path, monkeypatch, error
):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfgdir))
    fixture = tmp_path / "fixture.json"
    summary = SUMMARY if error else "Calculator shows 83,810,205 for 12,345 × 6,789."
    result = {
        "result": "83,810,205",
        "snapshot_id": SNAPSHOT,
        "label": RAW,
        "password": CREDENTIAL,
        "elements": [{"label": str(n)} for n in range(10)],
    }
    fixture.write_text(
        json.dumps(
            {
                "tools_pages": [
                    [
                        {
                            "name": "inspect",
                            "description": "Private app observation with requested-result summaries.",
                            "inputSchema": {"type": "object", "properties": {}},
                            "_meta": POLICY,
                        }
                    ]
                ],
                "tool_call_results": {
                    "inspect": {
                        "isError": error,
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                        ],
                    }
                },
            }
        )
    )
    server = Path(__file__).parent / "fixtures/mcp_servers/minimal_stdio_server.py"
    (cfgdir / "mcp.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "servers": {
                    "summary_fixture": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                        "env": {"ALYSIS_TEST_MCP_CONFIG": str(fixture)},
                        "trust": "explicit",
                    }
                },
            }
        )
    )
    cfg = config()
    cfg.stream = True
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="fullaccess",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="test",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "logs",
    )
    session.tools["tool_search"].run({"names": [NAME]})
    surface, hooks = _RecordingSurface(), _RecordingHooks()
    session.surface, session.hook_dispatcher = surface, hooks
    public = []
    session.tools["public_sink"] = ToolDef(
        name="public_sink",
        description="Public sink",
        parameters={},
        run=lambda args: public.append(copy.deepcopy(args)) or {"ok": True},
    )

    class Client:
        model = "mcp-vision-test"
        temperature = 0.2
        count = 0
        references = []

        def chat(self, *, messages, **kwargs):
            self.count += 1
            self.references.append(messages)
            assert kwargs.get("stream") is False
            if self.count == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="inspect-call", name=NAME, arguments={"action": "key"})
                    ],
                    raw={},
                )
            if self.count == 2:
                assert RAW in json.dumps(messages, ensure_ascii=False)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="public-call", name="public_sink", arguments={"text": RAW})
                    ],
                    raw={"echo": RAW},
                )
            return LLMResponse(
                content=summary, tool_calls=[], raw={"echo": RAW, "password": CREDENTIAL}
            )

    client = Client()
    session.client = client
    try:
        assert (
            session.run_turn(
                "Use Calculator for my requested calculation and report whether it worked."
            )
            == 0
        )
        durable = json.dumps(
            [
                session.messages,
                client.references,
                surface.__dict__,
                hooks.payloads,
                session.store.path.read_text(),
            ],
            default=str,
            ensure_ascii=False,
        )
        assert summary in durable
        assert any(
            message.get("role") == "assistant" and message.get("content") == summary
            for message in session.messages
        )
        assert any(event.get("text") == summary for event in surface.message_events)
        for secret in (RAW, CREDENTIAL, SNAPSHOT):
            assert secret not in durable
        assert RAW not in json.dumps(public, ensure_ascii=False)
        assert session.tools[NAME].metadata["requested_result_summary"] is True
    finally:
        session.close()


@pytest.mark.parametrize(
    "raw", ['{"value":"private"}', '```json\n{"value":"private"}\n```', '[{"value":"private"}]']
)
def test_requested_summary_refuses_raw_json_envelopes(raw):
    response = LLMResponse(content=raw, tool_calls=[], raw={})
    assert (
        requested_result_summary_text(response, ())
        == "App details are private. Please check the app for the requested result."
    )
