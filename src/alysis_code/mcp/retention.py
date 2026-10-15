"""Tool-declared retention, negotiated before any call arguments are observed."""

from typing import Any


def ephemeral_tool_io(raw_tool: dict[str, Any]) -> bool:
    metadata = raw_tool.get("_meta")
    if not isinstance(metadata, dict) or "ai.alysis/retention" not in metadata:
        return False
    policy = metadata["ai.alysis/retention"]
    if (
        not isinstance(policy, dict)
        or type(policy.get("version")) is not int
        or policy
        not in (
            {"version": 1, "mode": "ephemeral"},
            {"version": 2, "mode": "ephemeral", "assistant_summary": "requested_results"},
        )
    ):
        # Never silently downgrade a server's explicit privacy requirement.
        raise ValueError("Unsupported MCP tool retention policy")
    return True


def requested_result_summary(raw_tool: dict[str, Any]) -> bool:
    return (
        ephemeral_tool_io(raw_tool)
        and raw_tool["_meta"]["ai.alysis/retention"].get("assistant_summary") == "requested_results"
    )
