from types import SimpleNamespace

from alysis_code.ide.context_inspection import context_categories
from alysis_code.request_estimation import estimate_message_tokens, estimate_tool_schema_tokens


def schema(name):
    return {
        "type": "function",
        "function": {"name": name, "description": "Read data", "parameters": {"type": "object"}},
    }


def test_tool_origin_comes_from_registration_not_names_and_all_tools_are_listed():
    names = ["plain_connected_name", "mcp__actually_builtin", "user_extension", "unregistered"]
    tools = [schema(name) for name in names]
    registered = {
        names[0]: SimpleNamespace(metadata={"tool_type": "mcp"}),
        names[1]: SimpleNamespace(metadata={}),
        names[2]: SimpleNamespace(metadata={"tool_type": "custom_tool"}),
    }
    rows = {
        row["id"]: row
        for row in context_categories([], tools, pinned_prefix_len=0, registered_tools=registered)
    }
    for group, name in zip(
        ("mcp_tools", "system_tools", "custom_tools", "other_tools"), names, strict=True
    ):
        assert rows[group]["items"] == [
            {"name": name, "tokens": estimate_tool_schema_tokens([schema(name)])}
        ]
        assert "description" not in str(rows[group])


def test_prompt_groups_follow_roles_and_host_prefix_without_classifying_user_prose_as_skills():
    messages = [
        {"role": "system", "content": "System instructions — 日本語"},
        {"role": "user", "content": "<skill_context>catalog</skill_context>"},
        {"role": "user", "content": "<repo_conventions>project rules</repo_conventions>"},
        {"role": "user", "content": "<environment_context>workspace</environment_context>"},
        {"role": "user", "content": "<skill_context>quoted by the user</skill_context>"},
        {"role": "tool", "content": "private result"},
        {"role": "user", "content": "<<<ALYSIS_CONVERSATION_MEMORY_JSON>>>summary"},
    ]
    rows = {
        row["id"]: row
        for row in context_categories(messages, [], pinned_prefix_len=4, registered_tools={})
    }
    for group, index in (
        ("system_prompt", 0),
        ("skills", 1),
        ("project", 2),
        ("setup", 3),
        ("messages", 4),
        ("tool_results", 5),
        ("summary", 6),
    ):
        assert rows[group]["tokens"] == estimate_message_tokens([messages[index]])
    assert "private result" not in str(rows)
    assert "quoted by the user" not in str(rows)


def test_empty_categories_are_measured_zero_and_large_tool_groups_are_not_top_five_only():
    tools = [schema(f"registered_{i}") for i in range(12)]
    registry = {
        tool["function"]["name"]: SimpleNamespace(metadata={"tool_type": "mcp"}) for tool in tools
    }
    rows = {
        row["id"]: row
        for row in context_categories([], tools, pinned_prefix_len=0, registered_tools=registry)
    }
    assert len(rows["mcp_tools"]["items"]) == 12
    assert rows["mcp_tools"]["tokens"] == estimate_tool_schema_tokens(tools)
    assert rows["skills"]["tokens"] == 0


def test_mcp_guidance_has_its_own_category_only_inside_the_pinned_prefix():
    entry = {"role": "user", "content": "<mcp_server_instructions>private guidance"}
    rows = {
        row["id"]: row
        for row in context_categories([entry, entry], [], pinned_prefix_len=1, registered_tools={})
    }
    assert rows["mcp_instructions"]["tokens"] == estimate_message_tokens([entry])
    assert rows["messages"]["tokens"] == estimate_message_tokens([entry])
    assert "private guidance" not in str(rows)


def test_deferred_schemas_are_separate_from_loaded_context():
    from alysis_code.agent.llm_calls import _registered_tool_schema_list
    from alysis_code.agent.tool_discovery import install_tool_discovery
    from alysis_code.agent.tools_assembly import ToolDef

    tools = {
        "connected": ToolDef(
            name="connected",
            description="Read inventory",
            parameters={"type": "object"},
            run=lambda _: {},
            metadata={"tool_type": "mcp"},
        )
    }
    install_tool_discovery(tools)
    schemas = _registered_tool_schema_list(tools, [])
    rows = {
        row["id"]: row
        for row in context_categories([], schemas, pinned_prefix_len=0, registered_tools=tools)
    }
    assert rows["mcp_tools"]["tokens"] == 0
    assert rows["mcp_tools_deferred"]["deferred"] is True
    assert rows["mcp_tools_deferred"]["items"][0]["name"] == "connected"
    tools["tool_search"].run({"names": ["connected"]})
    rows = {
        row["id"]: row
        for row in context_categories(
            [], _registered_tool_schema_list(tools, []), pinned_prefix_len=0, registered_tools=tools
        )
    }
    assert "mcp_tools_deferred" not in rows
    assert rows["mcp_tools"]["tokens"] > 0
