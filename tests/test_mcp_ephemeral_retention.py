from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from test_mcp_visual_delivery import config
from test_sensitive_tool_output_boundary import _RecordingHooks, _RecordingSurface

from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.metadata import (
    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
)
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.mcp.retention import ephemeral_tool_io
from alysis_code.runtime_kind import RuntimeKind

POLICY = {"ai.alysis/retention": {"version": 1, "mode": "ephemeral"}}
NAME = "mcp__private_fixture__unfamiliar_surface"
SECRET = "PRIVATE_UI_Καλημέρα_日本語_8be01"
SNAPSHOT = "opaque-snapshot-00042"
RESULT_SECRET = "PRIVATE_RESULT_91fa"


def test_explicit_unknown_retention_is_not_downgraded():
    assert not ephemeral_tool_io({})
    assert ephemeral_tool_io({"_meta": POLICY})
    for value in (
        None,
        True,
        {"version": True, "mode": "ephemeral"},
        {"version": 2, "mode": "ephemeral"},
    ):
        with pytest.raises(ValueError, match="Unsupported"):
            ephemeral_tool_io({"_meta": {"ai.alysis/retention": value}})


@pytest.mark.parametrize("outcome", ["success", "tool_error", "provider_failure", "cancelled"])
@pytest.mark.parametrize("native_replay", ["none", "openai", "anthropic", "gemini"])
def test_private_stdio_calls_cross_the_real_agent_boundary(
    tmp_path, monkeypatch, outcome, native_replay
):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    fixture = tmp_path / "fixture.json"
    result = {"snapshot": SNAPSHOT, "label": RESULT_SECRET, "value": "0"}
    fixture.write_text(
        json.dumps(
            {
                "tools_pages": [
                    [
                        {
                            "name": "unfamiliar_surface",
                            "description": "A private surface.",
                            "inputSchema": {"type": "object", "properties": {}},
                            # This fixture only returns synthetic data. Declare its
                            # actual access so approval does not prevent the call
                            # whose retention boundaries this test exercises.
                            "annotations": {"readOnlyHint": True},
                            "_meta": POLICY,
                        }
                    ]
                ],
                "tool_call_results": {
                    "unfamiliar_surface": {
                        "isError": outcome == "tool_error",
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    }
                },
            }
        )
    )
    server = Path(__file__).parent / "fixtures/mcp_servers/minimal_stdio_server.py"
    (cfg_dir / "mcp.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "servers": {
                    "private_fixture": {
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
        # Authorize the fixture calls; this suite exercises retention after dispatch.
        # MCP approval decisions are covered by the permission-policy tests.
        mode="fullaccess",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="test",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "logs",
    )
    session.tools["tool_search"].run({"names": [NAME]})
    assert session.tools[NAME].metadata["ephemeral_io"] is True
    surface, hooks = _RecordingSurface(), _RecordingHooks()
    session.surface, session.hook_dispatcher = surface, hooks
    dispatched, public_arguments, usage = [], [], []
    original_run = session.tools[NAME].run

    def run(arguments):
        dispatched.append(copy.deepcopy(arguments))
        return original_run(arguments)

    session.tools[NAME] = replace(session.tools[NAME], run=run)
    session.tools["public_sink"] = ToolDef(
        name="public_sink",
        description="Test public sink",
        parameters={},
        run=lambda args: public_arguments.append(copy.deepcopy(args)) or {"ok": True},
    )
    record_usage = session._record_llm_usage

    def capture_usage(**kwargs):
        usage.append(copy.deepcopy(kwargs))
        record_usage(**kwargs)

    monkeypatch.setattr(session, "_record_llm_usage", capture_usage)
    event = threading.Event()
    token = EventCancellationToken(event)
    first = {"snapshot": SNAPSHOT, "arbitrary_field": SECRET, "coordinate": 0}
    second = {**first, "arbitrary_field": SECRET + "-second"}

    class Client:
        model = "mcp-vision-test"
        temperature = 0.2

        def __init__(self):
            self.references = []
            self.count = 0

        def chat(self, *, messages, **kwargs):
            self.count += 1
            self.references.append(messages)
            assert kwargs.get("stream") is False
            assert kwargs.get("on_reasoning_delta") is None
            if self.count == 1:
                calls = [ToolCall(id=SECRET, name=NAME, arguments=first)]
            elif self.count == 2:
                live = json.dumps(messages, ensure_ascii=False)
                assert RESULT_SECRET in live and SNAPSHOT in live and SECRET in live
                if outcome == "provider_failure":
                    raise RuntimeError("Disconnected " + RESULT_SECRET)
                if outcome == "cancelled":
                    event.set()
                    token.throw_if_cancelled()
                calls = [
                    ToolCall(id="second-call", name=NAME, arguments=second),
                    ToolCall(
                        id="public-call", name="public_sink", arguments={"text": RESULT_SECRET}
                    ),
                ]
            else:
                return LLMResponse(content="Finished observing.", tool_calls=[], raw={})
            metadata = None
            if native_replay == "openai":
                metadata = {
                    OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                        "response_id": f"private-response-{self.count}",
                        "output_items": [
                            {
                                "type": "function_call",
                                "id": f"fc-{index}",
                                "call_id": call.id,
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            }
                            for index, call in enumerate(calls)
                        ],
                    }
                }
            elif native_replay == "anthropic":
                metadata = {
                    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY: {
                        "message_id": f"private-message-{self.count}",
                        "content_blocks": [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                            for call in calls
                        ],
                    }
                }
            elif native_replay == "gemini":
                metadata = {
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "response_id": f"private-response-{self.count}",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "thoughtSignature": "private-signature",
                                    "functionCall": {
                                        "id": call.id,
                                        "name": call.name,
                                        "args": call.arguments,
                                    },
                                }
                                for call in calls
                            ],
                        },
                    }
                }
            return LLMResponse(
                content=SECRET, tool_calls=calls, raw={"echo": SECRET}, provider_metadata=metadata
            )

    client = Client()
    session.client = client
    try:
        if outcome == "provider_failure":
            with pytest.raises(RuntimeError) as failure:
                session.run_turn("Inspect the private surface.", cancellation_token=token)
            assert RESULT_SECRET not in str(failure.value)
        elif outcome == "cancelled":
            with pytest.raises(CooperativeCancellationError):
                session.run_turn("Inspect the private surface.", cancellation_token=token)
        else:
            assert session.run_turn("Inspect the private surface.", cancellation_token=token) == 0
            assert dispatched == [first, second]
            assert RESULT_SECRET not in json.dumps(public_arguments)
        assert dispatched[0] == first
        durable = json.dumps(
            [
                session.messages,
                client.references,
                surface.__dict__,
                hooks.payloads,
                usage,
                session.store.path.read_text(),
            ],
            default=str,
            ensure_ascii=False,
        )
        for secret in (SECRET, SNAPSHOT, RESULT_SECRET):
            assert secret not in durable
    finally:
        session.close()
