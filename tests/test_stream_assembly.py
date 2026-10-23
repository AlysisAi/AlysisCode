"""Stream assembly: chunks are appended as received unless a provider opts in.

The de-duplication in ``_stream_delta_suffix`` was written for a provider that
re-sends the text already streamed. Run on every stream, it dropped legitimate
repeats from ordinary incremental ones, including inside tool-call arguments.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.llm import openai_compat
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface

_CODE = "def f(x):\n    if x:\n        return 1\n    return 0\n"


def _sse(events: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return body.encode("utf-8")


def _client(events: list[dict], **kwargs) -> OpenAICompatClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(events)
        )

    return OpenAICompatClient(
        base_url=kwargs.pop("base_url", "https://example.com/v1"),
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _content_events(pieces: list[str]) -> list[dict]:
    return [{"choices": [{"index": 0, "delta": {"content": piece}}]} for piece in pieces]


def _argument_events(pieces: list[str]) -> list[dict]:
    events = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        }
    ]
    events += [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]},
                }
            ]
        }
        for piece in pieces
    ]
    return events


_REPEATS = [
    pytest.param(["    ", "    ", "return 1"], "        return 1", id="two_indents_then_code"),
    pytest.param(["aa", "aa", "aa", "aa"], "aaaaaaaa", id="four_identical_chunks"),
    pytest.param(["ha", "ha"], "haha", id="two_identical_chunks"),
]


@pytest.mark.parametrize(("pieces", "expected"), _REPEATS)
def test_default_assembly_keeps_every_content_chunk(pieces: list[str], expected: str) -> None:
    shown: list[str] = []
    client = _client(_content_events(pieces))

    response = client.chat(
        messages=[{"role": "user", "content": "hi"}], stream=True, on_text_delta=shown.append
    )

    assert response.content == expected
    assert shown == pieces


@pytest.mark.parametrize(("pieces", "expected"), _REPEATS)
def test_default_assembly_keeps_every_tool_argument_chunk(pieces: list[str], expected: str) -> None:
    arguments = ['{"content": "', *pieces, '"}']
    client = _client(_argument_events(arguments))

    response = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

    assert response.tool_calls[0].arguments == {"content": expected}


def test_default_assembly_rebuilds_code_arguments_split_like_tokens() -> None:
    arguments = json.dumps({"path": "a.py", "content": _CODE})
    pieces = re.findall(r" {4}| +|[^ ]{1,3}", arguments)
    assert "".join(pieces) == arguments
    client = _client(_argument_events(pieces))

    response = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

    assert response.tool_calls[0].arguments == {"path": "a.py", "content": _CODE}


def test_cumulative_assembly_still_trims_re_sent_text_when_opted_in() -> None:
    client = _client(
        _content_events(["I'll help.", "I'll help. Reading the file now."]),
        stream_assembly="cumulative",
    )

    response = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

    assert client.stream_assembly == "cumulative"
    assert response.content == "I'll help. Reading the file now."


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com/v1",
        "https://token-plan-ams.xiaomimimo.com/v1",
        "https://api.xiaomimimo.com/v1",
        "https://api.openai.com/v1",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://openrouter.ai/api/v1",
    ],
)
def test_no_provider_is_opted_in_to_cumulative_assembly(base_url: str) -> None:
    assert _client([], base_url=base_url).stream_assembly == "concat"


def test_resolve_stream_assembly() -> None:
    resolve_stream_assembly = openai_compat.resolve_stream_assembly
    assert resolve_stream_assembly(None, provider_key="qwen") == "concat"
    assert resolve_stream_assembly("auto", provider_key=None) == "concat"
    assert resolve_stream_assembly("CONCAT", provider_key=None) == "concat"
    assert resolve_stream_assembly("cumulative", provider_key=None) == "cumulative"
    with pytest.raises(ValueError, match="stream assembly must be one of"):
        resolve_stream_assembly("dedupe", provider_key=None)


def test_config_setting_defaults_to_auto_and_validates() -> None:
    assert AppConfig().llm_stream_assembly == "auto"
    assert (
        set_config_value(AppConfig(), "llm_stream_assembly", "Cumulative").llm_stream_assembly
        == "cumulative"
    )
    with pytest.raises(ConfigError, match="llm_stream_assembly must be one of"):
        set_config_value(AppConfig(), "llm_stream_assembly", "dedupe")


@pytest.mark.parametrize(
    ("setting", "expected"), [("auto", "concat"), ("cumulative", "cumulative")]
)
def test_factory_passes_the_setting_to_the_client(setting: str, expected: str) -> None:
    cfg = AppConfig(model="mimo-v2.5", base_url="https://token-plan-ams.xiaomimimo.com/v1")
    cfg.llm_stream_assembly = setting

    client = make_llm_client(cfg=cfg, api_key="key", model="mimo-v2.5")

    assert isinstance(client, OpenAICompatClient)
    assert client.stream_assembly == expected


def test_config_snapshot_records_the_stream_assembly_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    cfg = AppConfig(model="mimo-v2.5", base_url="https://token-plan-ams.xiaomimimo.com/v1")

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

    assert snapshots[0]["payload"]["effective"]["stream_assembly"] == "concat"
