from __future__ import annotations

MAX_MCP_TOOL_DESCRIPTION_CHARS = 2000

_STRIPPED_MCP_TOOL_DESCRIPTION_PREFIXES = (
    "ignore previous",
    "system:",
    "user:",
    "assistant:",
    "you are now",
    "new instructions:",
    "<system>",
    "</system>",
    "[inst]",
    "[/inst]",
)


def sanitize_mcp_tool_description(
    description: str, *, max_chars: int = MAX_MCP_TOOL_DESCRIPTION_CHARS
) -> str:
    kept_lines: list[str] = []
    for line in str(description or "").splitlines():
        normalized = line.lstrip().casefold()
        if any(normalized.startswith(prefix) for prefix in _STRIPPED_MCP_TOOL_DESCRIPTION_PREFIXES):
            continue
        kept_lines.append(line)
    body = "\n".join(kept_lines).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
    return f"[mcp-tool-description begin]\n{body}\n[mcp-tool-description end]"
