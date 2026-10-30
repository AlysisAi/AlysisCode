"""Legacy and selected Mac W7 availability, separate from W11 qualification.

This is not a preference, negotiated grant, or arbitrary-program sandbox.
Ordinary MCP, shell, embedded views and managed CDP have their own policies.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

UNAVAILABLE_MESSAGE = "Computer use is unavailable for this platform or route."
W11_UNAVAILABLE_MESSAGE = "This Computer Use route is not qualified."
CU_METHODS = frozenset(
    {
        "browser_start.respond",
        "computer_start_resolved.respond",
        "computer.respond",
        "task_budget.respond",
    }
)
CU_EVENTS = frozenset(
    {
        "computer_use_request",
        "computer_use_evidence",
        "computer_use_attribution",
        "task_budget_request",
        "browser_start_request",
        "computer_start_resolved_request",
    }
)
CU_FEATURES = frozenset(
    {
        "computer_use_evidence",
        "computer_use_attribution_v1",
        "task_budget",
        "computer_interaction_v2",
        "computer_application_lifecycle_v1",
        "computer_window_selection_v1",
        "computer_observation_v1",
        "computer_discovery_v1",
        "computer_use_start_resolved_v1",
        "browser_start_v1",
        "computer_use_host",
    }
)
CU_TOOLS = frozenset(
    {
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
        "computer_start_resolved_v1",
    }
)


# Historical W11 grouping: these selected older families are now Mac W7.
# Product availability never supplies W11/U2 qualification.
W11_FEATURES = frozenset(
    {
        "computer_interaction_v2",
        "computer_application_lifecycle_v1",
        "computer_window_selection_v1",
        "computer_use_start_resolved_v1",
    }
)
W11_METHODS = frozenset({"computer_start_resolved.respond"})
W11_EVENTS = frozenset({"computer_start_resolved_request"})
W11_HOST_FIELDS = frozenset(
    {"interaction_version", "application_lifecycle_version", "window_selection_version"}
)


def legacy_mac_computer_use_available() -> bool:
    """Product availability only. Read the real host, never IPC/config/env."""
    return sys.platform == "darwin"


def mac_w7_computer_use_available() -> bool:
    """Selected Mac W7 product surface; never a request/config/platform override."""
    return sys.platform == "darwin"


def refuse_unavailable_mac_w7_computer_use() -> None:
    if not mac_w7_computer_use_available():
        raise ValueError(W11_UNAVAILABLE_MESSAGE)


def w11_computer_use_qualified() -> bool:
    """Neither a legacy Mac session nor a platform label supplies qualification."""
    return False


def refuse_unqualified_computer_use() -> None:
    if not w11_computer_use_qualified():
        raise ValueError(W11_UNAVAILABLE_MESSAGE)


def computer_use_blocked() -> bool:
    """Compatibility spelling for the necessary legacy entry/serving callers."""
    return not legacy_mac_computer_use_available()


def unqualified_computer_arguments(
    name: str,
    arguments: Mapping[str, Any],
    *,
    interaction_v2: bool | None = None,
    application_lifecycle_v1: bool | None = None,
    window_selection_v1: bool | None = None,
) -> bool:
    """Deny new wire routes before host effects, including direct bridge callers."""
    if not isinstance(arguments, Mapping):
        return True
    if mac_w7_computer_use_available():
        if name == "computer_start_resolved_v1":
            return False  # Original descriptor/proof/owner validators still apply.
        if name in {"computer_state", "computer_action", "computer_wait"}:
            # Direct bridge callers supply original negotiated host flags. Tool
            # wrappers leave them absent and retain their own original validators
            # and typed refusal/recovery results. No model value supplies a flag.
            if arguments.get("action") in ("semantic", "focused_input") or "target" in arguments:
                return True
            if (
                arguments.get("action") == "quit_application"
                and interaction_v2 is not None
                and not (interaction_v2 and application_lifecycle_v1)
            ):
                return True
            if (
                {"list_windows", "window_ref"}.intersection(arguments)
                and interaction_v2 is not None
                and not (interaction_v2 and window_selection_v1)
            ):
                return True
            if {
                "target_type",
                "surface_ref",
                "frame_id",
                "recipient_ref",
                "input_mode",
            }.intersection(arguments) and interaction_v2 is False:
                return True
            if "input_mode" in arguments and arguments["input_mode"] != "background":
                return True
            for field, version, negotiated in (
                ("interaction_version", 2, interaction_v2),
                ("application_lifecycle_version", 1, interaction_v2 and application_lifecycle_v1),
                ("window_selection_version", 1, interaction_v2 and window_selection_v1),
            ):
                if (
                    interaction_v2 is not None
                    and field in arguments
                    and (
                        not negotiated
                        or type(arguments[field]) is not int
                        or arguments[field] != version
                    )
                ):
                    return True
            return False
    return (
        name == "computer_start_resolved_v1"
        or bool(W11_HOST_FIELDS.intersection(arguments))
        or (
            name in {"computer_state", "computer_action", "computer_wait"}
            and (
                bool(
                    {
                        "target_type",
                        "surface_ref",
                        "frame_id",
                        "recipient_ref",
                        "list_windows",
                        "window_ref",
                        "input_mode",
                        "target",
                    }.intersection(arguments)
                )
                or arguments.get("action") in ("semantic", "focused_input", "quit_application")
            )
        )
    )


def refuse_computer_use() -> None:
    """Refuse before a builder, callback, resource read or launch can act."""
    if computer_use_blocked():
        raise ValueError(UNAVAILABLE_MESSAGE)


def is_computer_use_tool(name: str, metadata: Mapping[str, Any]) -> bool:
    # The ordinary managed-CDP tool also has the name browser_start. Only the
    # host-owned handoff variant belongs to this fixed CU product boundary.
    return name in CU_TOOLS or (name == "browser_start" and metadata.get("host_owned") is True)


def computer_use_tool_blocked(name: str, metadata: Mapping[str, Any]) -> bool:
    return (name == "computer_start_resolved_v1" and not mac_w7_computer_use_available()) or (
        computer_use_blocked() and is_computer_use_tool(name, metadata)
    )
