from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .discovery import (
    CustomToolCapabilities,
    CustomToolDiscoveryResult,
    CustomToolIssue,
    CustomToolSpec,
    discover_custom_tools,
)
from .trust import ProjectToolTrustState, is_project_tool_trusted, load_trust_state

_CUSTOM_TOOL_TOP_LEVEL_RUNTIMES = {
    RuntimeKind.INTERACTIVE_CHAT,
    RuntimeKind.ONE_SHOT,
}
_CUSTOM_TOOL_TOP_LEVEL_RUNTIME_VALUES = frozenset(
    runtime_kind.value for runtime_kind in _CUSTOM_TOOL_TOP_LEVEL_RUNTIMES
)


@dataclass(frozen=True)
class CustomToolCatalogEntry:
    name: str
    source_scope: str
    trust: str
    status: str
    source_path: Path
    detail: str = ""
    manifest_version: int | None = None
    capabilities: CustomToolCapabilities | None = None
    spec: CustomToolSpec | None = None
    issue: CustomToolIssue | None = None
    exposed: bool = False

    @property
    def normalized_name(self) -> str:
        return self.name.casefold()


@dataclass(frozen=True)
class CustomToolSessionState:
    discovery: CustomToolDiscoveryResult
    trust_state: ProjectToolTrustState
    catalog_entries: tuple[CustomToolCatalogEntry, ...]
    effective_tools_by_name: dict[str, CustomToolSpec]
    exposed_tools_by_name: dict[str, CustomToolSpec]


def build_custom_tool_session_state(
    *,
    workspace_root: Path,
    custom_tools_enabled: bool,
    mode: str,
    runtime_kind: RuntimeKind | str,
    built_in_tool_names: set[str],
    catalog_view: bool = False,
    write_scope_restricted: bool = False,
    user_config_dir: Path | None = None,
    discovery: CustomToolDiscoveryResult | None = None,
    trust_state: ProjectToolTrustState | None = None,
) -> CustomToolSessionState:
    resolved_runtime_kind = normalize_runtime_kind(
        runtime_kind, fallback=RuntimeKind.INTERACTIVE_CHAT
    )
    active_discovery = discovery or discover_custom_tools(
        workspace_root=workspace_root,
        built_in_tool_names=built_in_tool_names,
        user_config_dir=user_config_dir,
    )
    active_trust_state = trust_state
    if active_trust_state is None:
        try:
            active_trust_state = load_trust_state(user_config_dir=user_config_dir)
        except RuntimeError:
            active_trust_state = ProjectToolTrustState()

    effective = active_discovery.effective_tools_by_name()
    exposed: dict[str, CustomToolSpec] = {}
    entries: list[CustomToolCatalogEntry] = []
    shadowed_names = {tool.normalized_name for tool in active_discovery.shadowed_tools}

    for tool in active_discovery.effective_tools:
        status, trust_label, detail = _tool_status(
            tool=tool,
            custom_tools_enabled=custom_tools_enabled,
            mode=mode,
            runtime_kind=resolved_runtime_kind,
            catalog_view=catalog_view,
            write_scope_restricted=write_scope_restricted,
            trust_state=active_trust_state,
        )
        exposed_flag = status == "available" and not catalog_view
        if exposed_flag:
            exposed[tool.normalized_name] = tool
        entries.append(
            CustomToolCatalogEntry(
                name=tool.name,
                source_scope=tool.source_scope,
                trust=trust_label,
                status=status,
                source_path=tool.source_path,
                detail=detail,
                manifest_version=tool.manifest_version,
                capabilities=tool.capabilities,
                spec=tool,
                exposed=exposed_flag,
            )
        )

    for tool in active_discovery.shadowed_tools:
        entries.append(
            CustomToolCatalogEntry(
                name=tool.name,
                source_scope=tool.source_scope,
                trust="global" if tool.source_scope == "global" else "persistent",
                status="shadowed",
                source_path=tool.source_path,
                detail="overridden by a project custom tool with the same name",
                manifest_version=tool.manifest_version,
                capabilities=tool.capabilities,
                spec=tool,
            )
        )

    for issue in active_discovery.issues:
        issue_name = issue.tool_name.strip() or Path(issue.relative_tool_path).stem
        if issue_name.casefold() in shadowed_names:
            detail = issue.message
        else:
            detail = issue.message
        entries.append(
            CustomToolCatalogEntry(
                name=issue_name,
                source_scope=issue.source_scope,
                trust="-",
                status="invalid",
                source_path=issue.source_path,
                detail=detail,
                issue=issue,
            )
        )

    entries_sorted = tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.name.casefold(),
                0 if entry.status == "available" else 1,
                0 if entry.source_scope == "project" else 1,
                entry.source_path.as_posix().casefold(),
            ),
        )
    )
    return CustomToolSessionState(
        discovery=active_discovery,
        trust_state=active_trust_state,
        catalog_entries=entries_sorted,
        effective_tools_by_name=effective,
        exposed_tools_by_name=exposed,
    )


def _tool_status(
    *,
    tool: CustomToolSpec,
    custom_tools_enabled: bool,
    mode: str,
    runtime_kind: RuntimeKind,
    catalog_view: bool,
    write_scope_restricted: bool,
    trust_state: ProjectToolTrustState,
) -> tuple[str, str, str]:
    if tool.source_scope == "global":
        trust_label = "global"
        trusted = True
    else:
        trusted = is_project_tool_trusted(tool, state=trust_state)
        trust_label = "persistent" if trusted else "untrusted"

    if not custom_tools_enabled:
        return "disabled", trust_label, "custom tools are disabled in config"
    if catalog_view:
        if not _tool_enabled_in_any_supported_top_level_runtime(tool):
            return (
                "disabled",
                trust_label,
                "tool manifest does not enable any supported top-level runtime",
            )
        if tool.missing_env:
            missing = ", ".join(tool.missing_env)
            return "missing-env", trust_label, f"missing required env vars: {missing}"
        if not trusted:
            return "untrusted", trust_label, "project tool requires persistent trust"
        return "available", trust_label, ""
    if write_scope_restricted:
        return (
            "disabled",
            trust_label,
            "custom tools are unavailable when write scope is narrowed",
        )
    if not _custom_tools_supported_runtime(mode=mode, runtime_kind=runtime_kind):
        return "disabled", trust_label, "custom tools are unavailable in this runtime or mode"
    if runtime_kind.value not in set(tool.enabled_in):
        return "disabled", trust_label, "tool manifest disables this runtime"
    if tool.missing_env:
        missing = ", ".join(tool.missing_env)
        return "missing-env", trust_label, f"missing required env vars: {missing}"
    if not trusted:
        return "untrusted", trust_label, "project tool requires persistent trust"
    return "available", trust_label, ""


def _custom_tools_supported_runtime(*, mode: str, runtime_kind: RuntimeKind) -> bool:
    if str(mode or "").strip().lower() == "readonly":
        return False
    return runtime_kind in _CUSTOM_TOOL_TOP_LEVEL_RUNTIMES


def _tool_enabled_in_any_supported_top_level_runtime(tool: CustomToolSpec) -> bool:
    return any(
        runtime_kind in _CUSTOM_TOOL_TOP_LEVEL_RUNTIME_VALUES for runtime_kind in tool.enabled_in
    )
