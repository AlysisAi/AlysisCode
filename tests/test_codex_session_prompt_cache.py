"""The ChatGPT subscription route keeps one session's growing prefix on one cache."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.cache_policy import derive_prompt_cache_stream_key
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderRequestAuthorization, openai_codex
from alysis_code.surface.noop_surface import NoopSurface

_SCOPE = "offline-subscription-cache-scope"
_KEY_RE = re.compile(r"alysis:codex:[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def _offline_subscription(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))

    def fail_request(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Subscription cache tests must use MockTransport, never a provider.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_request)
    monkeypatch.setattr(openai_codex, "load_provider_token", lambda *_a, **_k: None)
    monkeypatch.setattr(
        openai_codex.OpenAICodexSubscriptionAuth, "list_models", lambda _self, **_k: ()
    )
    monkeypatch.setattr(
        openai_codex.OpenAICodexSubscriptionAuth, "route_credential_scope", lambda _self: _SCOPE
    )

    def authorize(_self, _url, *, expected_credential_scope=None, **_kwargs):
        assert expected_credential_scope in (None, _SCOPE)
        return ProviderRequestAuthorization(credential_scope=_SCOPE, headers={})

    monkeypatch.setattr(openai_codex.OpenAICodexSubscriptionAuth, "authorize_request", authorize)


def _cfg(mode: str = "manual") -> AppConfig:
    cfg = AppConfig(
        model="gpt-5.6-luna",
        prompt_cache_mode=mode,
        web_search_mode="off",
        skills_enabled=False,
        stream=True,
    )
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            auth_provider="openai-codex",
            default_model="gpt-5.6-luna",
            reasoning_effort="high",
        ),
    )
    set_active_profile(cfg, "chatgpt-codex")
    return cfg


def _sse(response: dict[str, Any]) -> httpx.Response:
    event = {"type": "response.completed", "response": response}
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(
            f"event: response.completed\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        ).encode(),
    )


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_consecutive_session_requests_share_instructions_tools_and_hashed_cache_key(
    tmp_path: Path, mode: str
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 1})
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "call-list-1",
                    "name": "fs_list",
                    "arguments": json.dumps({"path": "."}),
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Listed the folder."}],
                }
            ]
        return _sse(
            {
                "id": f"resp-{len(requests)}",
                "model": "gpt-5.6-luna",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 100, "output_tokens": 5},
            }
        )

    (tmp_path / "notes.txt").write_text("synthetic\n", encoding="utf-8")
    session = create_session(
        cfg=_cfg(mode),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        enable_compaction=False,
        subagents_enabled=False,
        verification_enabled=False,
        one_shot_execution=True,
        surface=NoopSurface(),
        session_id_override="20261028T000000Z_cachefix",
    )
    try:
        session.client._transport = httpx.MockTransport(handler)
        session.run_turn("List the files in this folder.")
        plans = session.client._wire_request_diagnostics
    finally:
        session.close()

    assert len(requests) >= 2
    first, second = requests[0], requests[1]
    # Default (Manual) and Automatic modes both send the session key on this route.
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert _KEY_RE.fullmatch(first["prompt_cache_key"])
    assert "cachefix" not in first["prompt_cache_key"]
    assert "prompt_cache_retention" not in first
    # The cacheable prefix is byte-stable across the turn's requests.
    assert first["instructions"] == second["instructions"]
    assert first["tools"] == second["tools"]
    # Per-turn host notes are a volatile suffix re-sent after new items, by design;
    # everything before them is a shared prefix.
    stable = list(first["input"])
    while stable and stable[-1].get("role") == "developer":
        stable.pop()
    assert stable and stable[-1].get("role") == "user"
    assert second["input"][: len(stable)] == stable
    assert second["input"][len(stable)]["type"] == "function_call"
    assert plans is not None


def test_wire_diagnostics_report_stable_instructions_and_tools_across_session_calls(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        enable_compaction=False,
        subagents_enabled=False,
        surface=NoopSurface(),
        session_id_override="20261028T000000Z_wirediag",
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _sse(
            {
                "id": f"resp-{len(sent)}",
                "model": "gpt-5.6-luna",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done"}],
                    }
                ],
            }
        )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "fs_list",
                "description": "List a folder.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Stable host policy"},
        {"role": "user", "content": "List the folder."},
    ]
    try:
        session.client._transport = httpx.MockTransport(handler)
        first = session.client.chat(messages=messages, tools=tools)
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "fs_list", "arguments": '{"path":"."}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": '{"entries":[]}'},
            ]
        )
        second = session.client.chat(messages=messages, tools=tools)
    finally:
        session.close()

    plan_1 = first.provider_metadata["openai_responses"]["request_plan"]
    plan_2 = second.provider_metadata["openai_responses"]["request_plan"]
    assert plan_1["wire_instructions_sha256"] == plan_2["wire_instructions_sha256"]
    assert plan_1["wire_tools_sha256"] == plan_2["wire_tools_sha256"]
    assert plan_2["wire_previous_instructions_unchanged"] is True
    assert plan_2["wire_previous_tools_unchanged"] is True
    assert plan_2["wire_previous_history_prefix_preserved"] is True
    assert sent[0]["prompt_cache_key"] == sent[1]["prompt_cache_key"]
    assert _KEY_RE.fullmatch(sent[0]["prompt_cache_key"])


def test_protected_session_stream_key_is_a_digest_never_the_raw_session_id() -> None:
    plain = derive_prompt_cache_stream_key(session_id="20261028T000000Z_raw")
    protected = derive_prompt_cache_stream_key(session_id="20261028T000000Z_raw", digest_only=True)
    again = derive_prompt_cache_stream_key(session_id="20261028T000000Z_raw", digest_only=True)
    other = derive_prompt_cache_stream_key(session_id="20261028T000000Z_other", digest_only=True)
    child = derive_prompt_cache_stream_key(
        session_id="child", parent_session_id="parent", digest_only=True
    )

    assert plain == "20261028T000000Z_raw"
    assert protected == again
    assert protected != other
    assert protected is not None and "raw" not in protected
    assert re.fullmatch(r"alysis:session:[0-9a-f]{24}", protected)
    assert child == derive_prompt_cache_stream_key(session_id="child", parent_session_id="parent")


def test_subscription_adapter_hashes_any_cache_key_and_is_idempotent() -> None:
    adapter = openai_codex.OpenAICodexSubscriptionAuth()
    payload = adapter.adapt_responses_payload(
        {"input": [], "prompt_cache_key": "raw-session", "prompt_cache_retention": "24h"}
    )
    key = payload["prompt_cache_key"]

    assert _KEY_RE.fullmatch(key)
    assert "raw-session" not in key
    assert "prompt_cache_retention" not in payload
    assert adapter.adapt_responses_payload(payload)["prompt_cache_key"] == key
    assert "prompt_cache_key" not in adapter.adapt_responses_payload(
        {"input": [], "prompt_cache_key": "  "}
    )
