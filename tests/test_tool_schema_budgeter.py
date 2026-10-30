from __future__ import annotations

import json

import pytest

from alysis_code.context.tool_schema_budgeter import (
    analyze_tool_schema_budget,
    compact_custom_mcp_tool_parameters,
    tool_schema_signature,
)


@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        [None, "invalid", 7],
        [{"type": "function", "function": {"name": "δοκιμή", "description": "中文 👩🏽‍💻"}}],
        [
            {"type": "function", "function": {"name": "one", "parameters": {"enum": ["a", "b"]}}},
            None,
            {"type": "function", "function": {"name": "two", "description": "other"}},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "mcp__synthetic__lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "enum": ["Ελληνικά", "中文"]},
                            "items": {"type": "array", "items": {"type": "number"}},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {"name": "custom__synthetic", "description": "👩🏽‍💻"},
            },
        ],
    ],
)
def test_total_only_estimation_preserves_report_total_without_ranking_work(monkeypatch, tools):
    from alysis_code.context import tool_schema_budgeter
    from alysis_code.request_estimation import estimate_tool_schema_tokens

    expected = analyze_tool_schema_budget(tools).total_tokens
    original = tool_schema_budgeter.estimate_tokens
    counted = []

    def estimate(text):
        counted.append(text)
        return original(text)

    monkeypatch.setattr(tool_schema_budgeter, "estimate_tokens", estimate)
    assert estimate_tool_schema_tokens(tools) == expected
    valid_tools = [tool for tool in (tools or []) if isinstance(tool, dict)]
    assert counted == (
        [json.dumps(valid_tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
        if valid_tools
        else []
    )


def test_tool_schema_signature_is_stable_across_dict_key_order() -> None:
    first = [
        {
            "type": "function",
            "function": {
                "name": "search_rg",
                "description": "Search files",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                },
            },
        }
    ]
    second = [
        {
            "function": {
                "parameters": {
                    "properties": {"pattern": {"type": "string"}},
                    "type": "object",
                },
                "description": "Search files",
                "name": "search_rg",
            },
            "type": "function",
        }
    ]

    assert tool_schema_signature(first) == tool_schema_signature(second)


def test_tool_schema_budget_reports_overage_and_largest_tools() -> None:
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "small_tool",
                "description": "Small schema",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": " ".join(["large schema field"] * 200),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "include_context": {"type": "boolean"},
                    },
                },
            },
        },
    ]

    report = analyze_tool_schema_budget(tool_list, budget_tokens=1, largest_count=1)

    assert report.tool_count == 2
    assert report.total_tokens > 1
    assert report.over_budget
    assert report.over_budget_tokens == report.total_tokens - 1
    assert [entry.name for entry in report.largest_tools] == ["large_tool"]


def test_tool_schema_budget_classifies_tool_families() -> None:
    report = analyze_tool_schema_budget(
        [
            {"type": "function", "function": {"name": "mcp__github__search"}},
            {"type": "function", "function": {"name": "custom_deploy"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]
    )

    families = {entry.name: entry.family for entry in report.largest_tools}
    assert families["mcp__github__search"] == "mcp"
    assert families["custom_deploy"] == "custom"
    assert families["read_file"] == "builtin"


def test_compact_custom_mcp_tool_parameters_preserves_validation_keywords() -> None:
    schema = {
        "type": "object",
        "title": "Verbose schema",
        "description": "Root description",
        "default": {"mode": "safe"},
        "examples": [{"mode": "fast"}],
        "properties": {
            "default": {
                "type": "string",
                "description": "A legitimate argument named default.",
                "default": "kept-as-property-only",
            },
            "description": {
                "type": "string",
                "title": "A legitimate argument named description.",
            },
            "mode": {
                "type": "string",
                "description": "Mode description",
                "default": "safe",
                "enum": ["fast", "safe", "audit"],
            },
            "dry_run": {
                "type": "boolean",
                "const": True,
                "$comment": "Annotation",
            },
        },
        "required": ["mode"],
    }

    compacted = compact_custom_mcp_tool_parameters(schema)

    assert compacted == {
        "type": "object",
        "properties": {
            "default": {
                "type": "string",
            },
            "description": {
                "type": "string",
            },
            "mode": {
                "type": "string",
                "enum": ["fast", "safe", "audit"],
            },
            "dry_run": {
                "type": "boolean",
                "const": True,
            },
        },
        "required": ["mode"],
    }


def test_compact_custom_mcp_tool_parameters_keeps_object_enum_values_verbatim() -> None:
    schema = {
        "type": "object",
        "properties": {
            "profile": {
                "type": "object",
                "description": "Pick a profile",
                "enum": [
                    {"title": "Fast", "mode": 1},
                    {"description": "Careful", "mode": 2, "extras": [{"default": "keep"}]},
                ],
            },
            "enum": {
                "type": "string",
                "description": "A legitimate argument named enum.",
            },
        },
    }

    compacted = compact_custom_mcp_tool_parameters(schema)

    assert compacted["properties"]["profile"] == {
        "type": "object",
        "enum": [
            {"title": "Fast", "mode": 1},
            {"description": "Careful", "mode": 2, "extras": [{"default": "keep"}]},
        ],
    }
    assert compacted["properties"]["enum"] == {"type": "string"}
    assert compacted["properties"]["profile"]["enum"] is not schema["properties"]["profile"]["enum"]
    assert (
        compacted["properties"]["profile"]["enum"][0]
        is not schema["properties"]["profile"]["enum"][0]
    )


def test_compact_custom_mcp_tool_parameters_keeps_object_const_values_verbatim() -> None:
    schema = {
        "type": "object",
        "properties": {
            "pinned": {
                "type": "object",
                "const": {
                    "title": "Pinned",
                    "examples": ["x"],
                    "nested": {"default": "keep", "description": "keep too"},
                },
            },
        },
    }

    compacted = compact_custom_mcp_tool_parameters(schema)

    assert compacted["properties"]["pinned"] == {
        "type": "object",
        "const": {
            "title": "Pinned",
            "examples": ["x"],
            "nested": {"default": "keep", "description": "keep too"},
        },
    }
    assert compacted["properties"]["pinned"]["const"] is not schema["properties"]["pinned"]["const"]
