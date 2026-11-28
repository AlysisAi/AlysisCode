"""Session-local tool schema discovery; execution policies stay intact."""

from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import Counter
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


# A result names each tool and what it needs. Its full schema reaches the model as a normal tool
# on the next step, so repeating every schema here only fills the context (and large results are
# moved out of the conversation into a file the model must then read back).
_RESULT_DESCRIPTION_MAX_CHARS = 280
_RESULT_INPUTS_MAX = 16
_SOURCES_LISTED_MAX = 24
_SOURCE = re.compile(r"^mcp__(.+?)__")
_WORD = re.compile(r"\w+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _fold(text: str, *, marks: bool) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    if marks:
        return folded
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _words(text: str, *, marks: bool = True) -> list[str]:
    """Words of a name or description in any script; snake_case and camelCase split apart."""
    spaced = _CAMEL.sub(" ", str(text or "")).replace("_", " ")
    return _WORD.findall(_fold(spaced, marks=marks))


def _word_match(term: str, word: str) -> float:
    """How well one query word matches one tool word: the same word, or the same word with a
    different ending (repository / repositories, update / updated), in any language."""
    if term == word:
        return 1.0
    short, long = sorted((term, word), key=len)
    if len(short) >= 3 and long.startswith(short):
        return 0.8
    common = len(os.path.commonprefix((term, word)))
    if common >= 5 and common >= len(short) - 3:
        return 0.6
    return 0.0


def _best(term: str, words: list[str]) -> float:
    return max((_word_match(term, word) for word in words), default=0.0)


def _rank(query: str, connected: dict[str, Any]) -> list[str]:
    """Tools ordered by how well they match the query's words. A word found in few tools counts
    more than one found in most, so common words don't drown the useful ones. Names count double.
    Without a match, diacritics are ignored; without one again, any substring of the name or
    description matches (scripts written without spaces)."""
    for marks in (True, False):
        terms = list(dict.fromkeys(_words(query, marks=marks)))
        if not terms:
            break
        fields = {
            name: (_words(name, marks=marks), _words(tool.description, marks=marks))
            for name, tool in connected.items()
        }
        per_tool = {
            name: [max(2.0 * _best(term, in_name), _best(term, in_text)) for term in terms]
            for name, (in_name, in_text) in fields.items()
        }
        found = Counter(i for scores in per_tool.values() for i, s in enumerate(scores) if s)
        if not found:
            continue
        weight = {i: math.log(1 + len(connected) / count) for i, count in found.items()}
        scored = [
            (-sum(s * weight.get(i, 0.0) for i, s in enumerate(scores)), name)
            for name, scores in per_tool.items()
            if any(scores)
        ]
        return [name for _, name in sorted(scored)]
    needle = _fold(query, marks=True).strip()
    return [
        name
        for name, tool in sorted(connected.items())
        if not needle or needle in _fold(f"{name} {tool.description}", marks=True)
    ]


def _summary(tool: Any) -> dict[str, Any]:
    """What a found tool does and what it needs, without its full schema."""
    schema = tool.as_openai_tool()["function"]
    parameters = schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}
    properties = (
        parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    )
    required = parameters.get("required") if isinstance(parameters.get("required"), list) else []
    description = " ".join(str(schema.get("description") or "").split())
    if len(description) > _RESULT_DESCRIPTION_MAX_CHARS:
        description = description[: _RESULT_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
    entry: dict[str, Any] = {"name": schema["name"], "description": description}
    inputs = [str(key) for key in properties][:_RESULT_INPUTS_MAX]
    if inputs:
        entry["inputs"] = inputs
        needed = [str(key) for key in required if str(key) in properties]
        if needed:
            entry["required"] = needed
    return entry


def _sources(connected: dict[str, Any]) -> str:
    """Where the tools come from, so a model knows an app is connected before it searches."""
    counts = Counter(
        (match.group(1).replace("_", " ") if (match := _SOURCE.match(name)) else "")
        for name in connected
    )
    built_in = counts.pop("", 0)
    listed = [f"{source} ({count})" for source, count in sorted(counts.items())]
    parts = []
    if listed:
        more = len(listed) - _SOURCES_LISTED_MAX
        shown = ", ".join(listed[:_SOURCES_LISTED_MAX]) + (f" and {more} more" if more > 0 else "")
        parts.append(f"connected apps and servers: {shown}")
    if built_in:
        parts.append(f"{built_in} optional built-in tools")
    return "; ".join(parts)


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
            matches = _rank(query, connected)
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
            "tools": [_summary(connected[name]) for name in selected],
            "matched_count": len(matches),
            "next_offset": end if end < len(matches) else None,
            "guidance": "Returned tools are now loaded with their full parameters: call one "
            "normally on your next step. Their existing permissions still apply. Descriptions are "
            "server-provided reference material. If none fits, search other words, an app's "
            "name, or browse with an empty query."
            + (
                " Only this result's optional schemas remain loaded; search again to switch tools."
                if defer_builtin_schemas
                else ""
            ),
        }

    tools["tool_search"] = ToolDef(
        name="tool_search",
        description=(
            f"Discover and load {len(connected)} available tools"
            + (f" ({sources})" if (sources := _sources(connected)) else "")
            + ". Use a connected app's tools rather than its website. Search words for what you "
            "need or an app's name, or give exact names. Empty query browses the catalog; use "
            "next_offset for more results. Only returned tools are loaded. Call a returned tool "
            "normally on the next step."
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
