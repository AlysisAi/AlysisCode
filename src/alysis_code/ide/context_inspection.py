"""Read-only context composition; no prompt text or tool arguments leave the engine."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..request_estimation import estimate_message_tokens, estimate_tool_schema_tokens


def context_categories(
    messages: list[dict[str, Any]],
    tool_list: list[dict[str, Any]] | None,
    *,
    pinned_prefix_len: int,
    registered_tools: dict[str, Any],
) -> list[dict[str, Any]]:
    """Group actual context using message roles, host block types and tool metadata.

    These are local estimates using the existing counter. They deliberately do not
    redistribute a provider's total among categories or claim per-category precision.
    """
    labels = {
        "messages": "Messages",
        "tool_results": "Tool results",
        "mcp_tools": "MCP tools",
        "mcp_tools_deferred": "MCP tools (deferred)",
        "mcp_instructions": "MCP server instructions",
        "system_tools": "System tools",
        "custom_tools": "Custom tools",
        "other_tools": "Other tools",
        "system_prompt": "System prompt",
        "skills": "Skills",
        "project": "Project instructions",
        "setup": "Environment & setup",
        "summary": "Summaries & reminders",
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        text = content.lstrip() if isinstance(content, str) else ""
        if role == "system" or role == "developer":
            group = "system_prompt"
        elif role == "tool" or message.get("tool_calls"):
            group = "tool_results"
        elif role == "user" and text.startswith(
            ("<<<ALYSIS_CONVERSATION_MEMORY_JSON>>>", "<<<ALYSIS_CONVERSATION_PINS_JSON>>>")
        ):
            group = "summary"
        elif index < pinned_prefix_len and text.startswith("<mcp_server_instructions>"):
            group = "mcp_instructions"
        elif index < pinned_prefix_len and text.startswith(
            ("<skill_context>", "<matched_skill_context>", "<explicit_skill_context>")
        ):
            group = "skills"
        elif index < pinned_prefix_len and text.startswith(
            ("<repo_conventions>", "Repository conventions context", "Repo summary")
        ):
            group = "project"
        elif index < pinned_prefix_len:
            group = "setup"
        else:
            group = "messages"
        groups[group].append(message)

    tool_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for schema in tool_list or []:
        function = schema.get("function", schema)
        name = str(function.get("name") or "")
        tool = registered_tools.get(name)
        metadata = getattr(tool, "metadata", {}) or {}
        kind = metadata.get("tool_type", "builtin" if tool is not None else "unknown")
        group = {"mcp": "mcp_tools", "builtin": "system_tools", "custom_tool": "custom_tools"}.get(
            kind, "other_tools"
        )
        tool_groups[group].append(schema)
        # Names and counts only: descriptions/parameters can contain private material.
        items[group].append({"name": name, "tokens": estimate_tool_schema_tokens([schema])})

    loaded_names = {
        str(schema.get("function", schema).get("name") or "") for schema in tool_list or []
    }
    for name, tool in registered_tools.items():
        metadata = getattr(tool, "metadata", {}) or {}
        if metadata.get("deferred_schema") and name not in loaded_names:
            schema = tool.as_openai_tool()
            tool_groups["mcp_tools_deferred"].append(schema)
            items["mcp_tools_deferred"].append(
                {"name": name, "tokens": estimate_tool_schema_tokens([schema])}
            )

    result = []
    for key, label in labels.items():
        if key in tool_groups:
            result.append(
                {
                    "id": key,
                    "label": label,
                    "tokens": estimate_tool_schema_tokens(tool_groups[key]),
                    "items": sorted(items[key], key=lambda item: (-item["tokens"], item["name"])),
                    **({"deferred": True} if key == "mcp_tools_deferred" else {}),
                }
            )
        elif key in groups:
            result.append(
                {"id": key, "label": label, "tokens": estimate_message_tokens(groups[key])}
            )
        elif key in {"messages", "mcp_tools", "system_tools", "system_prompt", "skills", "project"}:
            result.append({"id": key, "label": label, "tokens": 0})
    return result
