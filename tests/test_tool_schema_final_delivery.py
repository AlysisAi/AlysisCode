"""Final model-facing tool schemas: keyword-named fields and unchanged built-in tools."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.agent import tools_assembly
from alysis_code.agent.browser_start import build_browser_start_tool, build_computer_start_tools
from alysis_code.agent.computer_start_resolved import Entry
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_RESPONSE_MODES,
    build_computer_use_tools,
)
from alysis_code.agent.llm_calls import _registered_tool_schema_list
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.context.tool_schema_budgeter import (
    JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS,
    JSON_SCHEMA_OPAQUE_DATA_KEYS,
    MODEL_FACING_SCHEMA_ANNOTATION_KEYS,
)
from alysis_code.custom_tools.discovery import _validate_input_schema
from alysis_code.llm.resolved_entry import NAME as RESOLVED_ENTRY_TOOL
from alysis_code.mcp.manager import _validate_function_tool_schema
from alysis_code.tools.registry import iter_builtin_tool_metadata
from tests.test_host_actions import _build_tools

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

KEYWORD_NAMES = sorted(JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS)


def _pre_fix_compact(value: Any, *, _inside_named_schema_map: bool = False) -> Any:
    """The budgeter as it was before the named-map fix, kept as the 'before' reference."""
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if not _inside_named_schema_map:
                if normalized_key in MODEL_FACING_SCHEMA_ANNOTATION_KEYS:
                    continue
                if normalized_key in JSON_SCHEMA_OPAQUE_DATA_KEYS:
                    compacted[key] = copy.deepcopy(item)
                    continue
            compacted[key] = _pre_fix_compact(
                item,
                _inside_named_schema_map=normalized_key in JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS,
            )
        return compacted
    if isinstance(value, list):
        return [_pre_fix_compact(item) for item in value]
    return value


def _delivered(schema: dict[str, Any], family: str) -> dict[str, Any]:
    if family == "mcp":
        parameters = _validate_function_tool_schema(
            server=SimpleNamespace(id="fixture"),
            tool=SimpleNamespace(name="fixture", input_schema=schema),
        )
    else:
        parameters = _validate_input_schema(schema)
    tool = ToolDef(
        name="fixture",
        description="Fixture",
        parameters=parameters,
        run=lambda _args: pytest.fail("Serialization must not dispatch."),
        metadata={"tool_type": family, "compact_parameters_for_model": True},
    )
    [wire] = _registered_tool_schema_list({"fixture": tool}, None)
    return json.loads(json.dumps(wire))["function"]["parameters"]


@pytest.mark.parametrize("family", ["mcp", "custom_tool"])
@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_final_schema_drops_annotations_of_keyword_named_fields(family: str, name: str) -> None:
    annotated = {
        "type": "string",
        "description": "ANNOTATION_SENTINEL",
        "title": "TITLE_SENTINEL",
        "examples": ["EXAMPLE_SENTINEL"],
    }
    schema = {
        "type": "object",
        "properties": {
            name: copy.deepcopy(annotated),
            "nested": {"type": "object", "properties": {name: copy.deepcopy(annotated)}},
        },
        "required": [name],
    }

    delivered = _delivered(schema, family)

    assert delivered["properties"][name] == {"type": "string"}
    assert delivered["properties"]["nested"]["properties"][name] == {"type": "string"}
    assert delivered["required"] == [name]
    assert "SENTINEL" not in json.dumps(delivered)


@pytest.mark.parametrize("family", ["mcp", "custom_tool"])
@pytest.mark.parametrize("kind", ["enum", "const"])
@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_final_schema_keeps_object_literals_of_keyword_named_fields(
    family: str, kind: str, name: str
) -> None:
    literal = {"description": "LITERAL", "title": "LITERAL", "nested": {"default": "LITERAL"}}
    value = [copy.deepcopy(literal)] if kind == "enum" else copy.deepcopy(literal)
    schema = {
        "type": "object",
        "properties": {name: {"type": "object", kind: copy.deepcopy(value)}},
        "required": [name],
    }

    delivered = _delivered(schema, family)

    assert delivered["properties"][name] == {"type": "object", kind: value}


def _every_built_in_tool(tmp_path: Path) -> dict[str, ToolDef]:
    tools: dict[str, ToolDef] = dict(_build_tools(tmp_path))
    for spec in iter_builtin_tool_metadata():
        tools.setdefault(
            spec.name,
            ToolDef(
                name=spec.name,
                description=spec.description,
                parameters=spec.copied_parameters(),
                run=lambda _args: {},
                metadata={"tool_type": "builtin", "compact_parameters_for_model": True},
            ),
        )
    for mode in COMPUTER_USE_RESPONSE_MODES:
        computer = build_computer_use_tools(
            lambda *_args: {},
            response_mode=mode,
            observation_available=True,
            interaction_v2=True,
            application_lifecycle_v1=True,
            window_selection_v1=True,
            verification_available=True,
            files_available=True,
            uploads_available=True,
        )
        tools.update({f"{name}@{mode}": tool for name, tool in computer.items()})
    tools["browser_start"] = build_browser_start_tool(lambda _request: None)
    tools.update(build_computer_start_tools(lambda _request: None, {"targets": []}))
    tools[RESOLVED_ENTRY_TOOL] = Entry.tool(SimpleNamespace(run=lambda _args: {}))
    return tools


def _final_schema_bytes(tools: dict[str, ToolDef]) -> dict[str, str]:
    return {
        key: json.dumps(_registered_tool_schema_list({key: tool}, None), ensure_ascii=False)
        for key, tool in tools.items()
    }


def test_every_built_in_and_computer_use_tool_schema_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _every_built_in_tool(tmp_path)
    names = {key.split("@", 1)[0] for key in tools}
    assert {
        "computer_state",
        "computer_action",
        "computer_wait",
        "computer_verify",
        "computer_download",
        "computer_file_status",
        "computer_upload",
        "computer_upload_status",
        "computer_discover",
        "computer_start",
        "browser_start",
        RESOLVED_ENTRY_TOOL,
    } <= names
    assert {spec.name for spec in iter_builtin_tool_metadata()} <= names

    after = _final_schema_bytes(tools)
    calls: list[Any] = []

    def before_fix(value: Any) -> Any:
        calls.append(value)
        return _pre_fix_compact(value)

    monkeypatch.setattr(tools_assembly, "compact_custom_mcp_tool_parameters", before_fix)
    before = _final_schema_bytes(tools)

    assert before == after
    # Built-in, browser and Computer Use tools never reach the custom/MCP budgeter.
    assert calls == []
