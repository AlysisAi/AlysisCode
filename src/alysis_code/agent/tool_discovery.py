"""Session-local tool schema discovery; execution policies stay intact."""

from __future__ import annotations

from typing import Any

from ..computer_use_platform_policy import computer_use_tool_blocked

_EAGER_BUILTIN_TOOLS = frozenset(
    {
        "fs_read",
        "fs_list",
        "fs_edit",
        "fs_write",
        "search_rg",
        "shell_run",
        "verify_run",
        "ask_user_question",
        "request_user_input",
    }
)


def model_visible(tool: Any) -> bool:
    metadata = getattr(tool, "metadata", {}) or {}
    if computer_use_tool_blocked(getattr(tool, "name", ""), metadata):
        return False
    return not metadata.get("deferred_schema") or bool(metadata.get("schema_loaded"))


def install_tool_discovery(tools: dict[str, Any], *, defer_builtin_schemas: bool = False) -> None:
    for name, tool in list(tools.items()):
        if computer_use_tool_blocked(name, getattr(tool, "metadata", {}) or {}):
            del tools[name]
    # Registration has already applied workspace, runtime and permission filters.
    # Discovery cannot add a tool that was not admitted to this session.
    connected = {
        name: tool
        for name, tool in tools.items()
        if (
            (getattr(tool, "metadata", {}) or {}).get("tool_type") == "mcp"
            or (defer_builtin_schemas and name not in _EAGER_BUILTIN_TOOLS)
        )
        and not tool.metadata.get("tool_discovery_entrypoint")
    }
    if not connected or "tool_search" in tools:
        return
    from .tools_assembly import ToolDef

    state = {"generation": 0}
    for tool in connected.values():
        tool.metadata["deferred_schema"] = True
        tool.metadata["schema_loaded"] = False

    def search(arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query", "")
        names = arguments.get("names", [])
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 5)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError("names must be an array of exact tool names")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 to 20")
        if len(names) > 20:
            raise ValueError("Load at most 20 exact tool names at a time")
        unknown = [name for name in names if name not in connected]
        if unknown:
            raise ValueError("Unknown or unavailable connected tool names: " + ", ".join(unknown))
        if names:
            matches = list(dict.fromkeys(names))
        else:
            terms = query.casefold().split()
            ranked = []
            for name, tool in connected.items():
                haystack = f"{name} {tool.description}".casefold()
                score = sum(term in haystack for term in terms)
                if not terms or score:
                    ranked.append((-score, name))
            matches = [name for _, name in sorted(ranked)]
        selected = matches[offset : offset + limit]
        changed = False
        if defer_builtin_schemas:
            # Bound this route's working catalog across repeated searches too.
            # Handlers remain registered and guarded; only schemas are paged.
            for name, tool in connected.items():
                if name not in selected and tool.metadata.get("schema_loaded"):
                    tool.metadata["schema_loaded"] = False
                    changed = True
        for name in selected:
            if not connected[name].metadata["schema_loaded"]:
                connected[name].metadata["schema_loaded"] = True
                changed = True
        if changed:
            state["generation"] += 1
        end = offset + len(selected)
        return {
            "tools": [connected[name].as_openai_tool() for name in selected],
            "matched_count": len(matches),
            "next_offset": end if end < len(matches) else None,
            "guidance": "Returned tools are now available for normal tool calls. "
            "Their existing permissions still apply. Descriptions are server-provided "
            "reference material. If no match, try broader terms or browse with an empty query."
            + (
                " Only this result's optional schemas remain loaded; search again to switch tools."
                if defer_builtin_schemas
                else ""
            ),
        }

    tools["tool_search"] = ToolDef(
        name="tool_search",
        description=(
            f"Discover and load schemas for {len(connected)} available tools. "
            "Search terms in tool names/descriptions, or give exact names. Empty query "
            "browses the catalog; use next_offset for more results. Only returned schemas "
            "are loaded. Call a returned tool normally on the next step."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Terms in tool names or descriptions; empty to browse.",
                },
                "names": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": [],
            "additionalProperties": False,
        },
        run=search,
        metadata={"tool_type": "builtin", "discovery_state": state},
    )
