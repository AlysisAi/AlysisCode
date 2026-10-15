"""Read-only desktop connector evidence; accounts and credentials stay with the host."""

from __future__ import annotations

import re
from typing import Any

CONNECTORS = (
    "github",
    "google_drive",
    "google_docs",
    "google_sheets",
    "google_slides",
    "google_calendar",
    "google_gmail",
    "google_chat",
    "google_contacts",
)
CONNECTION_TOOLS = (
    (
        "connectors.status",
        "connection_status",
        "Check the current selected account and plugin state for a desktop connector. "
        "Use for questions about account access or connection, even without a project. "
        "Report unknown or disconnected status honestly; a saved Google grant does not prove live API access. "
        "This does not connect accounts, grant permissions or read files. Do not check connections for greetings or unrelated questions.",
        {"connector": {"type": "string", "enum": list(CONNECTORS)}},
        ("connector",),
    ),
    (
        "connectors.github.repositories",
        "github_repositories",
        "Read one bounded page of repositories visible to the GitHub account selected in Alysis. "
        "No project folder is required. Includes accessible public, private and organization repositories; "
        "ungranted repositories remain invisible. Does not clone, switch accounts or modify anything. "
        "Use next_page to continue. Local Git operations still require a repository.",
        {"page": {"type": "integer", "minimum": 1, "maximum": 10000}},
        (),
    ),
)
CONNECTION_TOOL_NAMES = frozenset(spec[1] for spec in CONNECTION_TOOLS)


def connection_arguments(action: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = next(spec for spec in CONNECTION_TOOLS if spec[0] == action)
    if set(args) - set(spec[3]) or any(key not in args for key in spec[4]):
        raise ValueError("Invalid connection arguments.")
    if action == "connectors.status" and args["connector"] not in CONNECTORS:
        raise ValueError("Unsupported connector.")
    if "page" in args and (type(args["page"]) is not int or not 1 <= args["page"] <= 10000):
        raise ValueError("Invalid repository page.")
    return dict(args)


def _text(value: Any, limit: int = 254) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)
    )


def connection_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    if action == "connectors.status":
        required = {
            "connector",
            "plugin",
            "connection",
            "account",
            "evidence",
            "checked_at",
            "next_step",
        }
        if set(result) - required - {"reason"} or not required <= set(result):
            raise ValueError("Invalid connection result fields.")
        if any(not isinstance(result[key], str) for key in required - {"account"}) or (
            "reason" in result and not isinstance(result["reason"], str)
        ):
            raise ValueError("Invalid connection status types.")
        if (
            result["connector"] not in CONNECTORS
            or result["plugin"] not in {"enabled", "disabled", "not_installed", "unknown"}
            or result["connection"] not in {"connected", "disconnected", "unknown"}
            or result["evidence"] not in {"authenticated_identity", "saved_grant", "none"}
            or result["account"] is not None
            and not _text(result["account"])
            or not _text(result["checked_at"], 40)
            or result["next_step"]
            not in {
                "none",
                "install_plugin",
                "enable_plugin",
                "connect",
                "setup_required",
                "refresh_plugin",
                "install-github-cli",
                "authenticate-github-cli",
                "link-account",
                "retry",
            }
            or "reason" in result
            and result["reason"] not in {"plugin_status_unavailable", "account_status_unavailable"}
        ):
            raise ValueError("Invalid connection status.")
        if result["connection"] == "connected" and (
            result["account"] is None or result["evidence"] == "none"
        ):
            raise ValueError("Connected status requires account evidence.")
        if result["connection"] != "connected" and (
            result["account"] is not None or result["evidence"] != "none"
        ):
            raise ValueError("Unverified status must not claim an account.")
        if result["connection"] == "connected" and result["evidence"] != (
            "authenticated_identity" if result["connector"] == "github" else "saved_grant"
        ):
            raise ValueError("Connection evidence does not match the connector.")
        return dict(result)
    if set(result) != {"account", "repositories", "next_page"} or not _text(result["account"], 39):
        raise ValueError("Invalid repository result.")
    rows = result["repositories"]
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Repository list exceeds its bound.")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "url", "private"}
            or not isinstance(row["name"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["name"])
            or len(row["name"]) > 256
            or row["url"] != "https://github.com/" + row["name"]
            or type(row["private"]) is not bool
        ):
            raise ValueError("Invalid repository metadata.")
    page = result["next_page"]
    if page is not None and (type(page) is not int or not 1 <= page <= 10001):
        raise ValueError("Invalid next repository page.")
    return dict(result)
