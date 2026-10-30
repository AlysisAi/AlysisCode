from __future__ import annotations

import json

import pytest

import alysis_code.agent_loop as agent_loop
from alysis_code.agent.connector_result_evidence import ConnectorLinkEvidence
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.mcp.github_discovery import CALL, DESCRIBE, DISCOVER
from alysis_code.mcp.github_host import (
    GITHUB_MCP_ACTION,
    bind_host_mcp_catalog,
    normalize_host_mcp_catalog,
    wrap_mcp_result,
)
from alysis_code.run_outcome import task_outcome_notice, task_outcome_record
from alysis_code.surface import NoopSurface

pytest_plugins = ["tests.test_github_mcp_plugin"]
URL = "https://preview.example.net/demo/"
SOURCE_URL = "https://github.com/example/project/blob/main/index.html"


def raw_result(url=URL):
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "status": 201,
                        "result": {"html_url": url, "status": None},
                    }
                ),
            }
        ],
        "isError": False,
    }


@pytest.mark.parametrize("url", [URL, SOURCE_URL])
def test_operation_response_and_model_claim_cannot_certify_link(url):
    evidence = ConnectorLinkEvidence()
    evidence.observe("registered_connector", wrap_mcp_result("create_resource", raw_result(url)))
    assessed = evidence.assess(f"It is working: {url}")
    assert assessed["unchecked_links"] == [url]
    # Even unrelated passing local evidence cannot certify the unchecked external result.
    outcome = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={
            "connector_link_evidence": assessed,
            "completion_certificate": {"status": "SUFFICIENT"},
            "material_edit_count": 1,
            "accepted_verification_evidence": [{"passed": True}],
        },
    )
    assert not outcome["verified_success"]
    assert "connector_links_unverified" in outcome["problems"]


@pytest.mark.parametrize(
    "fetch",
    [
        {"status_code": 404, "content": "Not found"},
        {"status_code": 202, "content": ""},
        {"status_code": 200, "error": "provenance denied", "content": "unused"},
        {"status_code": 200, "content": ""},
        {"status_code": 200, "content": "body", "url": "https://unrelated.example.net/"},
    ],
)
def test_missing_failed_empty_or_unrelated_fetch_does_not_verify(fetch):
    evidence = ConnectorLinkEvidence()
    evidence.observe("registered_connector", wrap_mcp_result("create_resource", raw_result()))
    evidence.observe("web_fetch", {"url": URL, **fetch})
    assert evidence.assess(URL)["unchecked_links"] == [URL]


def test_fetch_is_current_observation_not_proof_of_the_arbitrary_goal():
    evidence = ConnectorLinkEvidence()
    evidence.observe("registered_connector", wrap_mcp_result("create_resource", raw_result()))
    evidence.observe("web_fetch", {"url": URL, "status_code": 200, "content": "Login page"})
    assert evidence.assess(URL) == {
        "unchecked_links": [],
        "fetched_links": [URL],
        "proves_requested_outcome": False,
    }
    evidence.observe("registered_connector", wrap_mcp_result("update_resource", raw_result()))
    assert evidence.assess(URL)["unchecked_links"] == [URL]
    assert ConnectorLinkEvidence().assess(URL)["unchecked_links"] == []


def test_unchecked_link_notice_preserves_other_verification_failures():
    notice = task_outcome_notice(
        {
            "verified_success": False,
            "problems": ["connector_links_unverified", "verification_failed"],
        }
    )
    assert "Link availability has not been verified" in notice
    assert "required or observed checks failed" in notice


@pytest.mark.parametrize(
    "url,fetch_status",
    [(SOURCE_URL, None), (URL, None), (URL, 404), (URL, 200), (URL, "unavailable")],
)
@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("repeat_write", [False, True])
def test_real_agent_loop_continues_once_then_retains_honest_outcome(
    installed,
    monkeypatch,
    url,
    fetch_status,
    protocol,
    repeat_write,
):
    catalog = normalize_host_mcp_catalog(
        {
            "protocol_version": 1,
            "plugin_id": "alysis.github",
            "server_id": "alysis.github/github",
            "tools": [
                {
                    "name": "create_resource",
                    "description": "Create a resource.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "readOnly": False,
                }
            ],
        }
    )
    writes = []
    prefix = [
        ToolCall(id="load", name="tool_search", arguments={"query": "resource"}),
        ToolCall(id="create", name="mcp__github__create_resource", arguments={}),
    ]
    capabilities = (GITHUB_MCP_ACTION,)
    discovered_tool = {
        "name": "create_resource",
        "description": "Create a resource.",
        "readOnly": False,
        "scope": "account",
        "schema_hash": "a" * 64,
    }
    if protocol == 2:
        catalog = normalize_host_mcp_catalog(
            {
                "protocol_version": 2,
                "plugin_id": "alysis.github",
                "server_id": "alysis.github/github",
                "discovery": {"version": 1},
            }
        )
        capabilities = (CALL, DISCOVER, DESCRIBE)
        prefix = [
            ToolCall(id="discover", name="github_discover", arguments={"query": "resource"}),
            ToolCall(
                id="describe",
                name="github_describe",
                arguments={
                    "catalog_id": "catalog_A",
                    "names": ["create_resource"],
                    "target": {"scope": "account"},
                },
            ),
            ToolCall(
                id="create",
                name="github_call",
                arguments={
                    "catalog_id": "catalog_A",
                    "target_id": "target_A",
                    "schema_hash": "a" * 64,
                    "name": "create_resource",
                    "arguments": {},
                },
            ),
        ]

    def host(action, args):
        if action == DISCOVER:
            return {"catalog_id": "catalog_A", "tools": [discovered_tool]}
        if action == DESCRIBE:
            return {
                "catalog_id": "catalog_A",
                "target_id": "target_A",
                "tools": [
                    {
                        **discovered_tool,
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
            }
        writes.append((action, args))
        return raw_result(url)

    def fetch(*, url, **kwargs):
        assert fetch_status is not None
        if fetch_status == "unavailable":
            raise RuntimeError("Synthetic web service unavailable")
        if fetch_status == 404:
            return {"url": url, "error": "HTTP 404", "error_code": "http_error"}
        return {"url": url, "final_url": url, "status_code": fetch_status, "content": "Demo page"}

    monkeypatch.setattr(agent_loop, "web_fetch", fetch)
    calls = []

    class FixtureSurface(NoopSurface):
        host_managed_approvals = True

    class Client:
        model = "test-model"
        temperature = 0.2

        def chat(self, *, messages, **kwargs):
            calls.append(messages)
            index = len(calls)
            if index <= len(prefix):
                return LLMResponse(
                    content="",
                    tool_calls=[prefix[index - 1]],
                    raw={},
                )
            if index == len(prefix) + 2:
                assert {t["function"]["name"] for t in kwargs["tools"]} == {"web_fetch"}
                context = "\n".join(
                    str(m.get("content", "")) for m in messages if m["role"] == "system"
                )
                assert "proposed reply includes connector-returned links" in context
                assert "source-file link does not establish" in context
                verification_calls = []
                if repeat_write:
                    verification_calls.append(
                        ToolCall(
                            id="fresh_valid_duplicate",
                            name=prefix[-1].name,
                            arguments=prefix[-1].arguments,
                        )
                    )
                if fetch_status is not None:
                    verification_calls.append(
                        ToolCall(id="check", name="web_fetch", arguments={"url": url})
                    )
                if verification_calls:
                    return LLMResponse(
                        content="",
                        tool_calls=verification_calls,
                        raw={},
                    )
            if index == len(prefix) + 3 and fetch_status == "unavailable":
                assert kwargs["tools"] == [], "Web failure must not restore mutation schemas"
            assert index <= len(prefix) + (3 if fetch_status is not None or repeat_write else 2), (
                "unbounded continuation"
            )
            return LLMResponse(content=url, tool_calls=[], raw={})

    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=installed,
        mode="fullaccess",
        yes=False,
        max_steps=8,
        no_log=False,
        api_key_override="fixture-key",
        surface=FixtureSurface(),
        enable_compaction=False,
        verification_enabled=False,
        session_log_dir_override=installed / "sessions",
        host_action_capabilities=capabilities,
        host_action_handler=host,
        host_mcp=bind_host_mcp_catalog(installed, catalog),
    )
    session.client = Client()
    try:
        assert session.run_turn("Create a simple hosted demo and give me its working link.") == 0
        assert len(writes) == 1
        assert session.mode == "fullaccess"
        events = session.store.events_snapshot()
        assert sum(e["type"] == "connector_link_verification_nudge" for e in events) == 1
        blocked = [e for e in events if e["type"] == "connector_verification_tool_blocked"]
        assert len(blocked) == int(repeat_write)
        final = [e["payload"] for e in events if e["type"] == "assistant_message"][-1]
        outcome = session.last_turn_outcome
        assert outcome["connector_link_evidence"]["proves_requested_outcome"] is False
        if fetch_status == 200:
            assert "Link availability has not been verified" not in final["content"]
            assert outcome["connector_link_evidence"]["fetched_links"] == [url]
        else:
            assert "Link availability has not been verified" in final["content"]
            assert outcome["outcome"] == "completed_unverified"
            assert outcome["connector_link_evidence"]["unchecked_links"] == [url]
        assert not outcome["verified_success"]

        # The host-generated verification phase cannot change the user's chosen
        # task authority. A later explicit user turn can execute another write.
        class NextTurnClient:
            model = "test-model"
            temperature = 0.2
            count = 0

            def chat(self, *, messages, **kwargs):
                self.count += 1
                assert prefix[-1].name in {t["function"]["name"] for t in kwargs["tools"]}
                if self.count == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="next_user_write",
                                name=prefix[-1].name,
                                arguments=prefix[-1].arguments,
                            )
                        ],
                        raw={},
                    )
                assert self.count == 2
                return LLMResponse(
                    content="The additional resource was created.", tool_calls=[], raw={}
                )

        session.client = NextTurnClient()
        assert session.run_turn("Create an additional resource.") == 0
        assert len(writes) == 2
        assert session.mode == "fullaccess"
    finally:
        session.close()
