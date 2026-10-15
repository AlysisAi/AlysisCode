from __future__ import annotations

import pytest

from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.mcp.github_host import (
    GITHUB_MCP_ACTION,
    bind_host_mcp_catalog,
    normalize_host_mcp_catalog,
)
from alysis_code.subagents import SubagentDefinition
from alysis_code.surface import NoopSurface
from tests.test_connector_result_completion import URL, raw_result

pytest_plugins = ["tests.test_github_mcp_plugin"]


@pytest.mark.parametrize(
    "tool_name,metadata,arguments,parallel,with_hooks",
    [
        ("shell_run", {"tool_type": "builtin"}, {"command": "synthetic"}, False, True),
        ("apply_patch", {"tool_type": "builtin"}, {}, False, True),
        ("subagent_spawn", {"tool_type": "builtin"}, {}, False, True),
        ("mcp__fixture__write", {"tool_type": "mcp", "readOnly": True}, {}, False, True),
        ("web_fetch", {"tool_type": "custom"}, {"url": URL}, False, True),
        (
            "subagent_run",
            {"tool_type": "builtin"},
            {"name": "worker", "task": "Check the result", "workspace_view": "isolated"},
            True,
            False,
        ),
    ],
)
def test_automatic_verification_blocks_other_dispatch_routes(
    installed,
    tool_name,
    metadata,
    arguments,
    parallel,
    with_hooks,
):
    """Real loop/parallel scheduler; synthetic effect handlers must never run."""
    catalog = normalize_host_mcp_catalog(
        {
            "protocol_version": 1,
            "plugin_id": "alysis.github",
            "server_id": "alysis.github/github",
            "tools": [
                {
                    "name": "create_resource",
                    "description": "Create a resource",
                    "inputSchema": {"type": "object", "properties": {}},
                    "readOnly": False,
                }
            ],
        }
    )
    writes, prohibited_effects, hook_tools = [], [], []

    def host(action, args):
        writes.append((action, args))
        return raw_result()

    class Surface(NoopSurface):
        host_managed_approvals = True

    class Hooks:
        def __getattr__(self, name):
            def hook(**kwargs):
                if name in {"fire_pre_tool_use", "fire_post_tool_use"}:
                    hook_tools.append(kwargs["tool_name"])
                return HookDispatchResult()

            return hook

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, *, messages, **kwargs):
            self.count += 1
            if parallel and self.count == 1:
                # Positive control: these real scheduler routes can dispatch
                # before automatic verification reduces the turn's capabilities.
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"normal_child_{i}",
                            name=tool_name,
                            arguments={**arguments, "task": f"Prepare resource {i}"},
                        )
                        for i in range(2)
                    ],
                    raw={},
                )
            index = self.count - int(parallel)
            if index == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="initial",
                            name="mcp__github__create_resource",
                            arguments={},
                        )
                    ],
                    raw={},
                )
            if index == 3:
                names = {t["function"]["name"] for t in kwargs["tools"]}
                assert names == (set() if tool_name == "web_fetch" else {"web_fetch"})
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"ignored_schema_{i}",
                            name=tool_name,
                            arguments=arguments,
                        )
                        for i in range(2 if parallel else 1)
                    ],
                    raw={},
                )
            assert index in {2, 4}
            return LLMResponse(content=URL, tool_calls=[], raw={})

    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=installed,
        mode="fullaccess",
        yes=False,
        max_steps=8,
        no_log=False,
        api_key_override="synthetic",
        surface=Surface(),
        enable_compaction=False,
        verification_enabled=False,
        session_log_dir_override=installed / "sessions",
        host_action_capabilities=(GITHUB_MCP_ACTION,),
        host_action_handler=host,
        host_mcp=bind_host_mcp_catalog(installed, catalog),
    )
    session.tools[tool_name] = ToolDef(
        name=tool_name,
        description="Synthetic effect handler",
        parameters={
            "type": "object",
            "properties": {name: {"type": "string"} for name in arguments},
            "additionalProperties": False,
        },
        metadata=metadata,
        run=lambda args: prohibited_effects.append(args) or {"status": "done"},
    )
    session.subagent_registry = {
        "worker": SubagentDefinition(
            name="worker", description="Fixture worker", system_prompt="Fixture", mode="fullaccess"
        ),
    }
    session.client = Client()
    original_scheduler = session.child_scheduler
    if parallel:
        # Exercise the production concurrent ToolDef dispatch path using only
        # synthetic handlers, without starting a real child/provider session.
        session.child_scheduler = None
    if with_hooks:
        session.hook_dispatcher = Hooks()
    try:
        assert session.run_turn("Create a resource and give me its verified link.") == 0
        assert len(writes) == 1
        assert len(prohibited_effects) == (2 if parallel else 0), [
            e["payload"] for e in session.store.events_snapshot() if e["type"] == "tool_result"
        ]
        if parallel:
            assert {p["task"] for p in prohibited_effects} == {
                "Prepare resource 0",
                "Prepare resource 1",
            }
        if with_hooks:
            assert "mcp__github__create_resource" in hook_tools
            assert tool_name not in hook_tools
        events = session.store.events_snapshot()
        blocked = [
            e["payload"] for e in events if e["type"] == "connector_verification_tool_blocked"
        ]
        assert len(blocked) == (2 if parallel else 1)
        assert {p["tool"] for p in blocked} == {tool_name}
        assert sum(e["type"] == "connector_link_verification_nudge" for e in events) == 1
        assert session.last_turn_outcome["outcome"] == "completed_unverified"
        assert session.last_turn_outcome["connector_link_evidence"]["unchecked_links"] == [URL]
        assert session.mode == "fullaccess"
    finally:
        session.child_scheduler = original_scheduler
        session.close()
