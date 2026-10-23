"""Safe, read-only integration snapshots and explicitly deferred backend actions.

The chat owner supplies its existing manager. Inspection must never construct a
manager or read ``tool_bindings``: both can start new connections. Call
``apply_pending_at_idle`` only inside the chat loop's serialized idle boundary;
checking a UI ``busy`` flag is not a synchronization mechanism.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...extensions.activation import resolve_active_plugins
from ...extensions.install import disable_plugin, enable_plugin
from ...extensions.state import load_global_state, load_project_overrides, load_project_state

Kind = Literal["mcp", "extensions"]
_LABEL = re.compile(r"[\w .:/-]{1,160}\Z", re.ASCII)


def _label(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    if not _LABEL.fullmatch(text) or any(secret and secret in text for secret in secrets):
        return "[redacted label]"
    return text


@dataclass(frozen=True)
class IntegrationRow:
    # The opaque key is used for selection, never rendered as a fallback label.
    key: str = field(repr=False)
    label: str
    configured_enabled: bool
    state: str
    tools: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    error: str = ""
    scope: str = "session"
    runtime_active: bool | None = None
    configuration_label: str = "Configuration"
    # Concise lifecycle/configuration notices for the collapsed inspector.
    # None preserves details supplied by callers that have no separate summary.
    summary: tuple[str, ...] | None = None


@dataclass(frozen=True)
class IntegrationSnapshot:
    kind: Kind
    rows: tuple[IntegrationRow, ...] = ()
    notices: tuple[str, ...] = ()


def inspect_mcp(
    manager: Any | None, *, configured_servers: Iterable[Any] = ()
) -> IntegrationSnapshot:
    """Refresh observations without connecting, authenticating, or rediscovering tools."""
    notices: list[str] = []
    if manager is None:
        servers = tuple(configured_servers)
        notices.append("Live MCP backend unavailable. Configured does not mean connected.")
    else:
        try:
            servers = tuple(manager.resolved_servers)
        except Exception:
            return IntegrationSnapshot(
                "mcp",
                notices=("MCP state unavailable. Check configuration and restart the session.",),
            )
    # Do not render arbitrary exception text or connection payloads. Even a
    # redacted URL can leave secrets in query strings, command arguments, etc.
    secrets = tuple(
        str(value)
        for server in servers
        for mapping in (getattr(server, "env", {}), getattr(server, "headers", {}))
        for value in mapping.values()
        if value
    )
    catalog: dict[str, Any] = {}
    if manager is not None:
        try:
            catalog = {
                entry["server_id"]: entry
                for entry in manager.catalog_snapshot_metadata().get("server_catalogs", ())
            }
        except Exception:
            notices.append(
                "Tool catalog unavailable. Refresh after reconnecting or restart the session."
            )
    rows: list[IntegrationRow] = []
    for server in servers:
        enabled = bool(server.enabled)
        state = "unavailable" if enabled else "disabled"
        error = ""
        actions: tuple[str, ...] = ()
        entry = catalog.get(server.id, {})
        tools = tuple(_label(name, secrets) for name in entry.get("exposed_tool_names", ()))
        details = ["Configuration changes require a new session."]
        if not enabled:
            details.append("Disabled in configuration; edit MCP configuration to enable.")
        elif manager is not None:
            if manager.closed:
                state = "disconnected"
            elif not server.enabled_for(manager.runtime_kind):
                state = "inactive for this runtime"
            elif server.trust != "explicit":
                state = "untrusted"
                details.append("Explicit MCP trust is required in configuration.")
            else:
                try:
                    status = manager.server_lifecycle_status(server_id=server.id)
                    if status.get("connection_state") == "failed":
                        # Its tools are not in this session; a restart cannot add them.
                        state = "failed"
                        error = (
                            "Did not start when this session began. Run `alysis mcp status` "
                            "for the reason, then start a new session."
                        )
                    else:
                        state = (
                            "disabled"
                            if not status.get("enabled")
                            else "connected"
                            if status.get("connected") is True
                            else "not connected"
                            if status.get("connection_state") == "not_materialized"
                            else "disconnected"
                        )
                        actions = ("disable", "restart") if status.get("enabled") else ("enable",)
                except Exception:
                    state = "failed"
                    error = "Connection state unavailable. Check MCP configuration and authentication; refresh or restart the session."
        if getattr(server, "oauth", None) is not None:
            details.append(
                "Authentication: use alysis mcp auth login outside chat, then restart this session."
            )
        if tools and state != "connected":
            details.append(
                "Listed tools belong to the frozen catalog and are currently unavailable."
            )
        if entry.get("tools_snapshot_stale"):
            details.append("Tool catalog changed; start a new session to load it.")
        rows.append(
            IntegrationRow(
                server.id,
                _label(server.id, secrets),
                enabled,
                state,
                tools,
                tuple(details),
                actions,
                error,
                runtime_active=(
                    bool(server.enabled_for(manager.runtime_kind)) if manager is not None else None
                ),
            )
        )
    return IntegrationSnapshot("mcp", tuple(rows), tuple(notices))


def inspect_extensions(repo_root: Path) -> IntegrationSnapshot:
    """Inspect installed components, honoring trust without prompting or granting it."""
    notices: list[str] = []
    records: list[tuple[str, Any]] = []
    user_enabled: set[str] = set()
    try:
        overrides = load_project_overrides(repo_root)
    except Exception:
        overrides = None
        notices.append("Project overrides unavailable. Check extension configuration.")
    for scope, loader in (
        ("user", load_global_state),
        ("project", lambda: load_project_state(repo_root)),
    ):
        try:
            loaded = loader()
            if scope == "user":
                user_enabled = {key.strip().lower() for key in getattr(loaded, "enabled", ())}
            records.extend((scope, (key, record)) for key, record in loaded.installed.items())
        except Exception:
            notices.append(
                f"{scope.capitalize()} extension state unavailable. Check extension configuration."
            )
    try:
        decision = resolve_active_plugins(repo_root=repo_root, workspace_trust_prompt=None)
    except Exception:
        decision = None
        notices.append(
            "Effective activation unavailable. Check extension configuration and workspace trust."
        )
    rows: list[IntegrationRow] = []
    for scope, (key, record) in records:
        normalized = key.strip().lower()
        enabled = decision is not None and normalized in decision.enabled_plugin_ids
        state = (
            "effective enabled (configuration)" if enabled else "effective disabled (configuration)"
        )
        if decision is None:
            state = "unknown"
        elif normalized in decision.untrusted_project_plugin_ids:
            state = "blocked by workspace trust"
        record_enabled = bool(getattr(record, "enabled", False))
        configured = (
            (record_enabled or normalized in user_enabled) if scope == "user" else record_enabled
        )
        override = (
            "enabled"
            if overrides is not None
            and normalized in {item.strip().lower() for item in overrides.enabled}
            else "disabled"
            if overrides is not None
            and normalized in {item.strip().lower() for item in overrides.disabled}
            else "absent"
            if overrides is not None
            else "unknown"
        )
        configuration_details = (
            f"Installed record flag: {'enabled' if record_enabled else 'disabled'}.",
            f"Project override: {override}. Trusted project overrides take precedence over user configuration.",
            "Effective activation is shared by plugin ID across scopes; it does not identify which installed copy is loaded.",
        )
        summary = [f"Project override: {override}."]
        if scope == "project":
            configuration_details += (
                "Enable/disable changes the project override, not the installed record flag. Project record flags do not determine effective activation.",
            )
        if (
            overrides is not None
            and (overrides.enabled or overrides.disabled)
            and not getattr(decision, "workspace_trust_granted", False)
        ):
            trust_notice = (
                "Project override trust is unknown; check configuration externally."
                if decision is None
                else "Project overrides are ignored until workspace trust is granted externally."
            )
            configuration_details += (trust_notice,)
            summary.append(
                "Override trust unknown; check configuration."
                if decision is None
                else "Project overrides need workspace trust."
            )
        summary.append("Restart the session after changes.")
        components = tuple(
            f"{_label(kind)}: {', '.join(_label(name) for name in names)}"
            for kind, names in sorted(record.component_ids.items())
        )
        rows.append(
            IntegrationRow(
                key,
                _label(key),
                configured,
                state,
                details=components
                + configuration_details
                + ("Activation is checked at session startup; restart after changes.",),
                actions=(("disable",) if configured else ("enable",))
                if scope == "user"
                else (("enable", "disable") if overrides is not None else ()),
                scope=scope,
                configuration_label="Installed record"
                if scope == "project"
                else "User configuration",
                summary=tuple(summary),
            )
        )
    return IntegrationSnapshot("extensions", tuple(rows), tuple(notices))


@dataclass(frozen=True)
class IntegrationAction:
    kind: Kind
    key: str = field(repr=False)
    action: str
    scope: str = "session"


@dataclass(frozen=True)
class ActionOutcome:
    status: str
    message: str


class IntegrationController:
    """UI requests are inert until the owning loop drains them while idle.

    Capture the manager identity with each request. A queued action must never
    migrate to another session/account when its manager provider changes.
    Closing/cancelling an inspector calls cancel_pending and leaves the draft
    untouched; this service never accesses the composer.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        manager_provider: Callable[[], Any | None],
        configured_servers_provider: Callable[[], Iterable[Any]] = lambda: (),
        extension_reader: Callable[[Path], IntegrationSnapshot] = inspect_extensions,
        extension_enable: Callable[..., Any] = enable_plugin,
        extension_disable: Callable[..., Any] = disable_plugin,
    ) -> None:
        self.repo_root = repo_root
        self._manager_provider = manager_provider
        self._configured_servers_provider = configured_servers_provider
        self._extension_reader = extension_reader
        self._extension_actions = {"enable": extension_enable, "disable": extension_disable}
        self._pending: tuple[IntegrationAction, Any] | None = None

    def manager_identity(self) -> Any | None:
        """Return the session-owned manager without querying or connecting it."""
        return self._manager_provider()

    def refresh(self, kind: Kind) -> IntegrationSnapshot:
        try:
            if kind == "extensions":
                return self._extension_reader(self.repo_root)
            manager = self._manager_provider()
            configured = self._configured_servers_provider() if manager is None else ()
            return inspect_mcp(manager, configured_servers=configured)
        except Exception:
            return IntegrationSnapshot(
                kind, notices=("Integration backend unavailable. Refresh or restart the session.",)
            )

    def request(self, action: IntegrationAction) -> ActionOutcome:
        if action.kind not in {"mcp", "extensions"} or action.action not in {
            "enable",
            "disable",
            "restart",
        }:
            return ActionOutcome(
                "unavailable",
                "This action is unavailable; use the documented external configuration or authentication flow.",
            )
        try:
            manager = self._manager_provider()
        except Exception:
            return ActionOutcome(
                "unavailable", "Live backend unavailable. Refresh or restart the session."
            )
        snapshot = inspect_mcp(manager) if action.kind == "mcp" else self.refresh("extensions")
        row = next(
            (row for row in snapshot.rows if row.key == action.key and row.scope == action.scope),
            None,
        )
        if row is None or action.action not in row.actions:
            return ActionOutcome(
                "unavailable", "Action unavailable for this integration. Refresh its state first."
            )
        if self._pending is not None:
            return ActionOutcome(
                "pending",
                "An integration change is already queued; cancel it before choosing another.",
            )
        self._pending = (action, manager)
        return ActionOutcome(
            "deferred",
            "Change queued for the next idle boundary. The current turn keeps its tools.",
        )

    def cancel_pending(self) -> ActionOutcome:
        self._pending = None
        return ActionOutcome("cancelled", "Integration change cancelled. Return to your draft.")

    def apply_pending_at_idle(self, *, turn_running: bool) -> ActionOutcome:
        if self._pending is None:
            return ActionOutcome("idle", "No pending integration change.")
        if turn_running:
            return ActionOutcome("deferred", "Waiting for the running turn to finish.")
        action, original_manager = self._pending
        self._pending = None
        try:
            manager = self._manager_provider()
            if manager is not original_manager:
                return ActionOutcome(
                    "cancelled", "Session changed. Refresh and request the change again."
                )
            if action.kind == "mcp":
                if manager is None or manager.closed:
                    return ActionOutcome(
                        "unavailable", "Live MCP backend unavailable. Restart the session."
                    )
                result = getattr(manager, f"{action.action}_server")(server_id=action.key)
                if action.action != "disable" and not result.get("connected"):
                    return ActionOutcome(
                        "failed",
                        "The backend did not establish a connection. Refresh and check configuration/authentication.",
                    )
                return ActionOutcome(
                    "applied", "Session connection updated. Refresh to inspect its current state."
                )
            self._extension_actions[action.action](
                plugin_id=action.key, repo_root=self.repo_root, project=action.scope == "project"
            )
            return ActionOutcome(
                "restart_required",
                "Extension configuration saved. Start a new session to apply the change.",
            )
        except Exception:
            return ActionOutcome(
                "failed",
                "Integration change failed. Check configuration/authentication; a changed tool catalog requires a new session.",
            )


def snapshot_lines(snapshot: IntegrationSnapshot) -> tuple[str, ...]:
    """Plain text only; UI adapters should not parse these lines as Rich markup."""
    lines = list(snapshot.notices)
    if not snapshot.rows:
        lines.append("No integrations available.")
    for row in snapshot.rows:
        lines.append(f"{row.label} [{row.scope}] — {row.state}")
        lines.append(
            f"  {row.configuration_label}: {'enabled' if row.configured_enabled else 'disabled'}"
        )
        if row.runtime_active is not None:
            lines.append(f"  Active for this runtime: {'yes' if row.runtime_active else 'no'}")
        if row.tools:
            lines.append("  Tools: " + ", ".join(row.tools))
        lines.extend("  " + detail for detail in row.details)
        if row.error:
            lines.append("  " + row.error)
    return tuple(lines)
