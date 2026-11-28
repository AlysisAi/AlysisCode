import copy
from dataclasses import replace

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_read_ledger_delivery_boundary import _Client, _response, _session

from alysis_code.agent.llm_calls import _registered_tool_schema_list
from alysis_code.agent.tool_discovery import install_tool_discovery
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.llm.types import LLMResponse, ToolCall


def tool(name, description="Inventory lookup", **metadata):
    return ToolDef(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        run=lambda _: {"observed": name},
        metadata={"tool_type": "mcp", **metadata},
    )


def test_session_installs_bounded_discovery_for_client_capacity_preference(tmp_path, monkeypatch):
    client = _Client([])
    client.prefers_deferred_tool_schemas = True
    session = _session(tmp_path, monkeypatch, client)
    try:
        from alysis_code.agent.tool_discovery import model_visible

        assert "tool_search" in session.tools
        assert model_visible(session.tools["fs_read"])
        assert model_visible(session.tools["shell_run"])
        optional = session.tools["shell_background"]
        handler = optional.run
        assert not model_visible(optional)
        loaded = session.tools["tool_search"].run({"names": ["shell_background"]})
        assert loaded["tools"][0]["name"] == "shell_background"
        assert model_visible(optional)
        assert optional.run is handler
    finally:
        session.close()


def names(tools):
    return [schema["function"]["name"] for schema in _registered_tool_schema_list(tools, [])]


def test_discovery_loads_only_results_and_preserves_registration():
    tools = {str(i): tool(str(i)) for i in range(12)}
    install_tool_discovery(tools)
    assert names(tools) == ["tool_search"]
    result = tools["tool_search"].run({"query": "Inventory", "limit": 2})
    assert result["matched_count"] == 12
    assert result["next_offset"] == 2
    assert set(names(tools)) == {"tool_search", "0", "1"}
    assert len(tools) == 13
    assert tools["0"].run({}) == {"observed": "0"}
    with pytest.raises(ValueError, match="unavailable"):
        tools["tool_search"].run({"names": ["not_authorized"]})


def test_search_supports_unicode_and_keeps_existing_discovery_entrypoints():
    tools = {
        "alpha": tool("alpha", "Αποθήκη 日本語"),
        "existing_discovery": tool("existing_discovery", tool_discovery_entrypoint=True),
    }
    install_tool_discovery(tools)
    assert set(names(tools)) == {"tool_search", "existing_discovery"}
    assert tools["tool_search"].run({"query": "日本語"})["tools"][0]["name"] == "alpha"
    # A script written without spaces still matches part of a word.
    assert tools["tool_search"].run({"query": "日本"})["tools"][0]["name"] == "alpha"


def test_loaded_schema_reaches_next_model_request_in_same_turn(tmp_path, monkeypatch):
    class RecordingClient(_Client):
        def __init__(self):
            super().__init__(
                [
                    _response(
                        ToolCall(id="find", name="tool_search", arguments={"query": "Inventory"})
                    ),
                    _response(ToolCall(id="lookup", name="connected_inventory", arguments={})),
                    LLMResponse(content="Inventory checked.", tool_calls=[], raw={}),
                ]
            )
            self.schemas = []

        def chat(self, **kwargs):
            self.schemas.append(copy.deepcopy(kwargs.get("tools")))
            return super().chat(**kwargs)

    client = RecordingClient()
    session = _session(tmp_path, monkeypatch, client)
    session.tools["connected_inventory"] = tool("connected_inventory")
    install_tool_discovery(session.tools)
    try:
        assert session.run_turn("Look up the inventory using the connected tools.") == 0
        request_names = [{t["function"]["name"] for t in schemas} for schemas in client.schemas]
        assert "connected_inventory" not in request_names[0]
        assert "tool_search" in request_names[0]
        assert "connected_inventory" in request_names[1]
        assert "connected_inventory" in {t["function"]["name"] for t in session.tool_list}
    finally:
        session.close()


def test_private_retention_engages_when_schema_is_loaded_not_merely_registered(
    tmp_path, monkeypatch
):
    class Client(_Client):
        def __init__(self):
            super().__init__(
                [
                    _response(
                        ToolCall(
                            id="find",
                            name="tool_search",
                            arguments={"names": ["private_inventory"]},
                        )
                    ),
                    _response(ToolCall(id="private", name="private_inventory", arguments={})),
                    LLMResponse(content="Inventory checked.", tool_calls=[], raw={}),
                ]
            )
            self.streams = []

        def chat(self, **kwargs):
            self.streams.append(kwargs["stream"])
            if len(self.streams) == 3:
                assert "PRIVATE_RECORD_49" in str(kwargs["messages"])
            return super().chat(**kwargs)

    client = Client()
    session = _session(tmp_path, monkeypatch, client)
    session.stream = True
    private = replace(
        tool("private_inventory", ephemeral_io=True), run=lambda _: {"value": "PRIVATE_RECORD_49"}
    )
    session.tools["private_inventory"] = private
    install_tool_discovery(session.tools)
    try:
        assert session.run_turn("Read connected inventory.") == 0
        assert client.streams == [True, False, False]
        assert "PRIVATE_RECORD_49" not in str(session.messages)
        assert "PRIVATE_RECORD_49" not in session.store.path.read_text()
    finally:
        session.close()


def test_results_name_each_tool_and_its_inputs_without_repeating_full_schemas():
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "x" * 4000}, "page": {}},
        "required": ["query"],
        "additionalProperties": False,
    }
    tools = {
        "mcp__github__search_repositories": ToolDef(
            name="mcp__github__search_repositories",
            description="Search repositories. " + "Long reference text. " * 200,
            parameters=schema,
            run=lambda _: {},
            metadata={"tool_type": "mcp"},
        )
    }
    install_tool_discovery(tools)
    found = tools["tool_search"].run({"query": "repositories"})["tools"]
    assert found == [
        {
            "name": "mcp__github__search_repositories",
            "description": found[0]["description"],
            "inputs": ["query", "page"],
            "required": ["query"],
        }
    ]
    assert len(found[0]["description"]) <= 280
    assert "x" * 100 not in str(found)
    # The full schema still reaches the model as a normal tool.
    assert set(names(tools)) == {"tool_search", "mcp__github__search_repositories"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("repositories", "mcp__code__list_repository"),
        ("updated issues", "mcp__tracker__issue_updates"),
        ("Αρχεία", "mcp__drive__find_files"),
        ("αρχειο", "mcp__drive__find_files"),
        ("listRepositories", "mcp__code__list_repository"),
    ],
)
def test_search_matches_words_with_other_endings_in_any_language(query, expected):
    tools = {
        "mcp__code__list_repository": tool("mcp__code__list_repository", "One repository"),
        "mcp__tracker__issue_updates": tool("mcp__tracker__issue_updates", "Recent update"),
        "mcp__drive__find_files": tool("mcp__drive__find_files", "Βρίσκει ένα αρχείο"),
        "mcp__mail__send": tool("mcp__mail__send", "Sends a message"),
    }
    install_tool_discovery(tools)
    result = tools["tool_search"].run({"query": query})
    assert result["tools"][0]["name"] == expected
    assert "mcp__mail__send" not in [entry["name"] for entry in result["tools"]]


def test_words_found_in_few_tools_outrank_words_found_in_most():
    tools = {
        f"mcp__app__tool_{i}": tool(f"mcp__app__tool_{i}", "Reads the account data")
        for i in range(8)
    }
    tools["mcp__app__invoices"] = tool("mcp__app__invoices", "Lists invoices for the account")
    install_tool_discovery(tools)
    result = tools["tool_search"].run({"query": "the account invoices", "limit": 1})
    assert result["tools"][0]["name"] == "mcp__app__invoices"


def test_discovery_says_which_apps_are_connected_before_any_search():
    tools = {
        **{f"mcp__github__t{i}": tool(f"mcp__github__t{i}") for i in range(3)},
        "mcp__hub_gmail__fetch": tool("mcp__hub_gmail__fetch"),
        "mcp__custom_acme__lookup": tool("mcp__custom_acme__lookup"),
    }
    install_tool_discovery(tools)
    description = tools["tool_search"].description
    assert "github (3)" in description
    assert "hub gmail (1)" in description
    assert "custom acme (1)" in description
    assert "rather than its website" in description
