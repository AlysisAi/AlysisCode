#!/usr/bin/env python3
"""Validate the CLI-to-VS Code backend parity contract.

The parity matrix is curated policy, but the feature inventory comes from code.
This script keeps the two aligned without importing the CLI or extension.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_MATRIX_PATH = REPO_ROOT / "docs" / "generated" / "ide_cli_parity_matrix.json"
PARITY_BURNDOWN_PATH = REPO_ROOT / "docs" / "generated" / "ide_cli_parity_burndown.md"
FORGE_PARITY_BURNDOWN_PATH = REPO_ROOT / "docs" / "generated" / "ide_forge_parity_burndown.md"
IDE_PROTOCOL_DOC_PATH = REPO_ROOT / "docs" / "ide_protocol.md"
VSCODE_EXTENSION_DOC_PATH = REPO_ROOT / "docs" / "vscode_extension.md"
CLI_ROOT_PATH = REPO_ROOT / "src" / "alysis_code" / "cli_impl" / "commands" / "root.py"
CHAT_COMMANDS_PATH = REPO_ROOT / "src" / "alysis_code" / "cli_impl" / "chat" / "commands.py"
CHAT_STATE_PATH = REPO_ROOT / "src" / "alysis_code" / "cli_impl" / "commands" / "chat_state.py"
IDE_HEALTH_PATH = REPO_ROOT / "src" / "alysis_code" / "ide" / "health.py"
IDE_STDIO_BRIDGE_PATH = REPO_ROOT / "src" / "alysis_code" / "ide" / "stdio_bridge.py"
IDE_MANAGEMENT_PROTOCOL_PATH = REPO_ROOT / "src" / "alysis_code" / "ide" / "management_protocol.py"
EXTENSION_PACKAGE_PATH = REPO_ROOT / "extensions" / "vscode-alysis" / "package.json"
EXTENSION_PROTOCOL_TS_PATH = (
    REPO_ROOT / "extensions" / "vscode-alysis" / "src" / "client" / "AlysisProtocol.ts"
)
EXTENSION_BRIDGE_CLIENT_TS_PATH = (
    REPO_ROOT / "extensions" / "vscode-alysis" / "src" / "client" / "AlysisBridgeClient.ts"
)
EXTENSION_BACKEND_ACTION_METADATA_PATH = (
    REPO_ROOT / "extensions" / "vscode-alysis" / "src" / "backend" / "BackendActionMetadata.ts"
)
EXTENSION_COMMAND_REGISTRY_TS_PATH = (
    REPO_ROOT / "extensions" / "vscode-alysis" / "src" / "commands" / "registry.ts"
)
EXTENSION_SRC_PATH = REPO_ROOT / "extensions" / "vscode-alysis" / "src"
EXTENSION_SLASH_REGISTRY_PATH = (
    REPO_ROOT / "extensions" / "vscode-alysis" / "src" / "slash" / "SlashCommandRegistry.ts"
)
IDE_PROTOCOL_METHOD_CONTRACT_PATH = REPO_ROOT / "docs" / "generated" / "ide_protocol_methods.json"

VALID_STATUSES = {
    "experimental_with_gate",
    "implemented_in_extension",
    "implemented_in_bridge_only",
    "planned_for_protocol",
    "intentionally_cli_only",
    "blocked_until_security_model",
    "not_applicable_to_ide",
}

VALID_EXTENSION_COMMAND_CATEGORIES = {
    "command_palette",
    "session_tree_context",
    "manage_tree_context",
    "manage_tree_title",
    # Commands the Cockpit dispatches programmatically (quick picks, webview actions). They are
    # contributed so the id stays declared, but carry no menus beyond a suppressed palette entry.
    "cockpit_programmatic",
}

VALID_EXTENSION_COMMAND_ROUTE_TYPES = {
    "registered_command_handler",
    "backend_action_group",
    "backend_action",
    "context_backend_action_dispatch",
    "tree_refresh",
}

# These responses are negotiated with the desktop's trusted host controller.
# VS Code does not advertise that host capability or own these approval/media
# boundaries; its typed client must not pretend to implement them.
DESKTOP_HOST_ONLY_METHODS = {
    "browser_start.respond",
    "computer.respond",
    "task_budget.respond",
    "host.action.dispatched",
    "session.userQuestions.get",
    "session.userQuestions.answer",
    "session.userQuestions.touch",
}

TS_CLIENT_COVERAGE_EXEMPT_METHODS = {
    "health",
    "getCapabilities",
}

COCKPIT_ROUTE_METHODS = {
    # ProviderCatalogController activation uses ChatController's idle transition gate.
    "session.setProfile",
    "session.fork",
    "initialize",
    "health",
    "getCapabilities",
    "session.create",
    "chat.send",
    "run.start",
    "session.cancel",
    "approval.respond",
    # HostActionController answers capability-negotiated Tasks/Debug requests
    # emitted by the backend; this is an event-response controller route, not a
    # command-palette action or BackendActionMetadata entry.
    "host.action.respond",
    "job.status",
    # ChatController routes the persona picker, /persona slash command, and the
    # persona chip through these directly; they are cockpit controller routes,
    # not BackendActionMetadata entries.
    "session.personas.list",
    "session.persona.set",
    "session.list",
    "session.getEvents",
    "artifact.list",
    "artifact.read",
    "forge.plan",
    "forge.plan.start",
    "forge.plan.result",
    "forge.list",
    "forge.open",
    "forge.resume",
    "forge.status",
    "forge.executePreview",
    "forge.execute",
    "forge.cancel",
    # SW6: the cockpit Run Swarm console routes these (renderSwarmConsole +
    # alysis.runSwarm command + swarm.* webview messages).
    "forge.swarm.start",
    "forge.swarm.status",
    "forge.swarm.result",
    "forge.swarm.cancel",
    "forge.swarm.review",
    "forge.swarm.apply",
    "forge.swarm.discard",
    # Durable recovery is a native Forge Cockpit picker with revision-fenced resume.
    "forge.swarm.list",
    "forge.swarm.resume",
    # BrowserCockpitController routes the complete managed-browser lifecycle.
    "browser.start",
    "browser.navigate",
    "browser.snapshot",
    "browser.screenshot",
    "browser.artifact.read",
    "browser.diagnostics",
    "browser.click",
    "browser.type",
    "browser.status",
    "browser.list",
    "browser.close",
    # CodeReviewPresenter publishes completed structured findings to Problems.
    "code.review.start",
    "code.review.result",
    "diff.list",
    "diff.get",
    # Models surface: ProviderCatalogController.verifyConnection routes the explicit-intent live
    # key check (connect / switch / replace-key), and configureProvider.ts routes it for the
    # command-palette key flow.
    "doctor.providers.live",
}

_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9-]*$|^/$")


class ExtractionError(RuntimeError):
    """Raised when static extraction cannot infer a command surface safely."""


@dataclass(frozen=True, order=True)
class FeatureRef:
    surface: str
    name: str


@dataclass(frozen=True)
class TyperCommand:
    app_var: str
    command: str
    source: Path
    function_name: str


@dataclass(frozen=True, order=True)
class CommandAliasGroup:
    surface: str
    canonical: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ExtensionCommandRoute:
    command: str
    category: str
    route_type: str
    backend_actions: tuple[str, ...]
    backend_action_group: str | None
    handler: str | None
    menus: tuple[str, ...]
    hidden_from_command_palette: bool
    rationale: str


@dataclass(frozen=True)
class PackageCommandMetadata:
    hidden_from_command_palette: bool
    menu_locations: tuple[str, ...]


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword_str(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _literal_str(keyword.value)
    return None


def _typer_command_name(decorator: ast.AST, function_name: str) -> tuple[str, str] | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute) or func.attr != "command":
        return None
    if not isinstance(func.value, ast.Name):
        raise ExtractionError("TODO: unsupported Typer command decorator target")

    raw_name = _literal_str(call.args[0]) if call is not None and call.args else None
    command_name = raw_name or function_name.replace("_", "-")
    if not command_name:
        raise ExtractionError(f"TODO: could not infer command name for {function_name}")
    return func.value.id, command_name


def _resolve_import_path(root_path: Path, module: str | None, level: int) -> Path:
    if level <= 0:
        raise ExtractionError("TODO: absolute command-module imports are not supported")
    package_dir = root_path.parent
    for _ in range(level - 1):
        package_dir = package_dir.parent
    module_parts = module.split(".") if module else []
    return package_dir.joinpath(*module_parts).with_suffix(".py")


def _root_import_paths(root_path: Path) -> dict[str, tuple[Path, str]]:
    imported: dict[str, tuple[Path, str]] = {}
    tree = _parse_python(root_path)
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level <= 0:
            continue
        module_path = _resolve_import_path(root_path, node.module, node.level)
        for alias in node.names:
            local_name = alias.asname or alias.name
            imported[local_name] = (module_path, alias.name)
    return imported


def _local_typer_apps(path: Path) -> set[str]:
    """Return Typer app variables declared directly in a command module."""

    apps: set[str] = set()
    tree = _parse_python(path)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        is_typer_factory = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "typer"
            and func.attr == "Typer"
        )
        if not is_typer_factory:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        apps.update(target.id for target in targets if isinstance(target, ast.Name))
    return apps


def _iter_typer_commands(path: Path) -> Iterable[TyperCommand]:
    tree = _parse_python(path)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = _typer_command_name(decorator, node.name)
            if parsed is None:
                continue
            app_var, command = parsed
            yield TyperCommand(
                app_var=app_var,
                command=command,
                source=path,
                function_name=node.name,
            )


def _iter_add_typer_calls(root_path: Path) -> Iterable[tuple[str, str, str]]:
    tree = _parse_python(root_path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_typer":
            continue
        if not isinstance(node.func.value, ast.Name):
            raise ExtractionError("TODO: unsupported add_typer parent expression")
        if not node.args or not isinstance(node.args[0], ast.Name):
            raise ExtractionError("TODO: unsupported add_typer child expression")
        group_name = _keyword_str(node, "name")
        if group_name is None:
            raise ExtractionError("TODO: add_typer without a static name must be classified")
        yield node.func.value.id, node.args[0].id, group_name


def _cli_registration_graph() -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[Path, str]],
    set[FeatureRef],
]:
    imported_paths = _root_import_paths(CLI_ROOT_PATH)
    local_apps = _local_typer_apps(CLI_ROOT_PATH)
    prefix_by_app: dict[str, tuple[str, ...]] = {"app": ()}
    module_by_app: dict[str, tuple[Path, str]] = {"app": (CLI_ROOT_PATH, "app")}
    group_features: set[FeatureRef] = set()

    unresolved = list(_iter_add_typer_calls(CLI_ROOT_PATH))
    while unresolved:
        next_unresolved: list[tuple[str, str, str]] = []
        progressed = False
        for parent_app, child_app, group_name in unresolved:
            parent_prefix = prefix_by_app.get(parent_app)
            if parent_prefix is None:
                next_unresolved.append((parent_app, child_app, group_name))
                continue
            child_prefix = (*parent_prefix, group_name)
            prefix_by_app[child_app] = child_prefix
            module_ref = imported_paths.get(child_app)
            if module_ref is None and child_app in local_apps:
                module_ref = (CLI_ROOT_PATH, child_app)
            if module_ref is None:
                raise ExtractionError(
                    f"TODO: cannot resolve module for Typer app {child_app!r} "
                    f"registered as {' '.join(child_prefix)!r}"
                )
            module_by_app[child_app] = module_ref
            group_features.add(FeatureRef("cli_command_group", " ".join(child_prefix)))
            progressed = True
        if not progressed:
            missing = ", ".join(f"{parent}->{child}" for parent, child, _name in next_unresolved)
            raise ExtractionError(f"TODO: cannot resolve nested Typer app registrations: {missing}")
        unresolved = next_unresolved

    return prefix_by_app, module_by_app, group_features


def _qualified_cli_commands_by_function() -> dict[tuple[Path, str, str], list[str]]:
    prefix_by_app, module_by_app, _group_features = _cli_registration_graph()
    commands_by_function: dict[tuple[Path, str, str], list[str]] = {}
    for app_var, (module_path, command_app_var) in sorted(module_by_app.items()):
        prefix = prefix_by_app[app_var]
        for command in _iter_typer_commands(module_path):
            if command.app_var != command_app_var:
                continue
            command_name = " ".join((*prefix, command.command))
            key = (module_path, command.app_var, command.function_name)
            commands_by_function.setdefault(key, []).append(command_name)
    return commands_by_function


def extract_cli_command_features() -> set[FeatureRef]:
    """Extract Typer command groups and commands from the CLI root registration graph."""

    _prefix_by_app, _module_by_app, group_features = _cli_registration_graph()
    command_features: set[FeatureRef] = set()
    for command_names in _qualified_cli_commands_by_function().values():
        for command_name in command_names:
            command_features.add(FeatureRef("cli_command", command_name))

    return group_features | command_features


def _canonical_cli_alias(command_names: list[str], function_name: str) -> str:
    function_suffix = function_name.rsplit("_", 1)[-1].replace("_", "-")
    for command_name in command_names:
        if command_name.rsplit(" ", 1)[-1] == function_suffix:
            return command_name
    return command_names[0]


def extract_cli_command_alias_groups() -> set[CommandAliasGroup]:
    groups: set[CommandAliasGroup] = set()
    for (_module_path, _app_var, function_name), command_names in sorted(
        _qualified_cli_commands_by_function().items(),
        key=lambda item: (item[0][0].as_posix(), item[0][1], item[0][2]),
    ):
        if len(command_names) < 2:
            continue
        canonical = _canonical_cli_alias(command_names, function_name)
        aliases = tuple(sorted(command for command in command_names if command != canonical))
        groups.add(
            CommandAliasGroup(
                surface="cli_command",
                canonical=canonical,
                aliases=aliases,
            )
        )
    return groups


class _SlashCompareVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.commands: set[str] = set()

    def visit_Compare(self, node: ast.Compare) -> Any:  # noqa: N802
        exprs = [node.left, *node.comparators]
        has_plain_cmd = any(isinstance(expr, ast.Name) and expr.id == "cmd" for expr in exprs)
        if has_plain_cmd:
            for expr in exprs:
                self._collect_slash_literals(expr)
        self.generic_visit(node)

    def _collect_slash_literals(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SLASH_COMMAND_RE.match(node.value):
                self.commands.add(node.value)
            return
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            for element in node.elts:
                self._collect_slash_literals(element)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ExtractionError(f"TODO: function {name!r} is missing")


def _extract_slash_commands_from_function(path: Path, function_name: str) -> set[str]:
    tree = _parse_python(path)
    function = _find_function(tree, function_name)
    visitor = _SlashCompareVisitor()
    visitor.visit(function)
    if not visitor.commands:
        raise ExtractionError(f"TODO: no slash commands inferred from {function_name}")
    return visitor.commands


def extract_cli_chat_slash_features() -> set[FeatureRef]:
    commands = _extract_slash_commands_from_function(CHAT_COMMANDS_PATH, "_handle_chat_command")
    commands.update(
        _extract_slash_commands_from_function(CHAT_STATE_PATH, "_parse_forge_enter_command")
    )
    return {FeatureRef("cli_chat_slash_command", command) for command in sorted(commands)}


def extract_cli_forge_chat_features() -> set[FeatureRef]:
    commands = _extract_slash_commands_from_function(
        CHAT_COMMANDS_PATH,
        "_handle_forge_chat_command",
    )
    return {FeatureRef("cli_forge_chat_command", command) for command in sorted(commands)}


def extract_ide_method_features() -> set[FeatureRef]:
    tree = _parse_python(IDE_HEALTH_PATH)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "SUPPORTED_METHODS"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            raise ExtractionError("TODO: SUPPORTED_METHODS must remain a static tuple/list")
        methods: set[FeatureRef] = set()
        for element in node.value.elts:
            method = _literal_str(element)
            if method is None:
                raise ExtractionError("TODO: non-literal IDE method in SUPPORTED_METHODS")
            methods.add(FeatureRef("ide_bridge_method", method))
        if not methods:
            raise ExtractionError("TODO: no IDE methods found in SUPPORTED_METHODS")
        return methods
    raise ExtractionError("TODO: SUPPORTED_METHODS is missing from health.py")


def extract_management_method_features() -> set[FeatureRef]:
    methods = _extract_static_string_sequence(
        IDE_MANAGEMENT_PROTOCOL_PATH,
        "MANAGEMENT_METHODS",
    )
    if not methods:
        raise ExtractionError("TODO: no management methods found in MANAGEMENT_METHODS")
    return {FeatureRef("ide_bridge_method", method) for method in sorted(methods)}


def extract_management_handler_features() -> set[FeatureRef]:
    tree = _parse_python(IDE_MANAGEMENT_PROTOCOL_PATH)
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "_HANDLERS" for target in node.targets
            ):
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "_HANDLERS":
                value = node.value
        if value is None:
            continue
        if not isinstance(value, ast.Dict):
            raise ExtractionError("TODO: _HANDLERS must remain a static dict")
        methods: set[FeatureRef] = set()
        for key in value.keys:
            method = _literal_str(key)
            if method is None:
                raise ExtractionError("TODO: non-literal management handler key")
            methods.add(FeatureRef("ide_bridge_method", method))
        if not methods:
            raise ExtractionError("TODO: no management handlers found in _HANDLERS")
        return methods
    raise ExtractionError("TODO: _HANDLERS is missing from management_protocol.py")


def _literal_string_container(node: ast.AST) -> set[str] | None:
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        values: set[str] = set()
        for element in node.elts:
            value = _literal_str(element)
            if value is None:
                return None
            values.add(value)
        return values
    return None


class _StdioDispatchVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.methods: set[str] = set()
        self.includes_management_methods = False

    def visit_Compare(self, node: ast.Compare) -> Any:  # noqa: N802
        left = node.left
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            self._collect_pair(left, op, comparator)
            left = comparator
        self.generic_visit(node)

    def _collect_pair(self, left: ast.AST, op: ast.cmpop, right: ast.AST) -> None:
        if isinstance(op, ast.Eq):
            if isinstance(left, ast.Name) and left.id == "method":
                value = _literal_str(right)
                if value is not None:
                    self.methods.add(value)
            elif isinstance(right, ast.Name) and right.id == "method":
                value = _literal_str(left)
                if value is not None:
                    self.methods.add(value)
            return

        if isinstance(op, ast.In) and isinstance(left, ast.Name) and left.id == "method":
            container_values = _literal_string_container(right)
            if container_values is not None:
                self.methods.update(container_values)
                return
            if isinstance(right, ast.Name) and right.id == "MANAGEMENT_METHODS":
                self.includes_management_methods = True
                return
            raise ExtractionError("TODO: unsupported method dispatch membership test")


def extract_stdio_dispatch_method_features() -> set[FeatureRef]:
    tree = _parse_python(IDE_STDIO_BRIDGE_PATH)
    dispatch = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_dispatch":
            dispatch = node
            break
    if dispatch is None:
        raise ExtractionError("TODO: StdioBridge._dispatch is missing")

    visitor = _StdioDispatchVisitor()
    visitor.visit(dispatch)
    methods = {FeatureRef("ide_bridge_method", method) for method in visitor.methods}
    if visitor.includes_management_methods:
        methods.update(extract_management_method_features())
    if not methods:
        raise ExtractionError("TODO: no stdio bridge dispatch methods inferred")
    return methods


def _extract_static_string_sequence(path: Path, name: str) -> set[str]:
    tree = _parse_python(path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            raise ExtractionError(f"TODO: {name} must remain a static tuple/list/set")
        values: set[str] = set()
        for element in node.value.elts:
            value = _literal_str(element)
            if value is None:
                raise ExtractionError(f"TODO: non-literal entry in {name}")
            values.add(value)
        return values
    raise ExtractionError(f"TODO: {name} is missing from {path.relative_to(REPO_ROOT)}")


def extract_ts_protocol_method_features() -> set[FeatureRef]:
    text = EXTENSION_PROTOCOL_TS_PATH.read_text(encoding="utf-8")
    return {
        FeatureRef("ide_bridge_method", ref.name)
        for ref in sorted(extract_ide_method_features())
        if ref.name in text
    }


def extract_ts_bridge_client_method_features() -> set[FeatureRef]:
    text = EXTENSION_BRIDGE_CLIENT_TS_PATH.read_text(encoding="utf-8")
    methods = {
        *re.findall(r"\brequest\(\s*\"([^\"]+)\"", text),
        *re.findall(r"\bmanagementRequest\(\s*\"([^\"]+)\"", text),
    }
    if not methods:
        raise ExtractionError("TODO: no bridge-client request methods inferred")
    return {FeatureRef("ide_bridge_method", method) for method in sorted(methods)}


def extract_backend_action_method_features() -> set[FeatureRef]:
    text = EXTENSION_BACKEND_ACTION_METADATA_PATH.read_text(encoding="utf-8")
    advertised_methods = {ref.name for ref in extract_ide_method_features()}
    methods = {
        match
        for match in re.findall(r'"([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+)"', text)
        if match in advertised_methods
    }
    if not methods:
        raise ExtractionError("TODO: no backend action required methods inferred")
    return {FeatureRef("ide_bridge_method", method) for method in sorted(methods)}


def extract_protocol_contract_method_features() -> set[FeatureRef]:
    payload = json.loads(IDE_PROTOCOL_METHOD_CONTRACT_PATH.read_text(encoding="utf-8"))
    methods: set[str] = set()
    for group in payload.get("method_groups", []):
        group_methods = group.get("methods")
        if not isinstance(group_methods, list) or not all(
            isinstance(method, str) for method in group_methods
        ):
            raise ValueError("ide_protocol_methods.json method_groups must list methods")
        methods.update(group_methods)
    raw_methods = payload.get("methods")
    if not isinstance(raw_methods, dict):
        raise ValueError("ide_protocol_methods.json must contain a methods object")
    methods.update(raw_methods)
    return {FeatureRef("ide_bridge_method", method) for method in sorted(methods)}


def extract_extension_command_features() -> set[FeatureRef]:
    payload = json.loads(EXTENSION_PACKAGE_PATH.read_text(encoding="utf-8"))
    contributes = payload.get("contributes")
    if not isinstance(contributes, dict):
        raise ExtractionError("TODO: package.json contributes object is missing")
    features: set[FeatureRef] = set()
    for entry in contributes.get("commands", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            raise ExtractionError(
                "TODO: package.json command entries must have literal command ids"
            )
        features.add(FeatureRef("extension_command", entry["command"]))
    for participant in contributes.get("chatParticipants", []):
        if not isinstance(participant, dict):
            raise ExtractionError("TODO: package.json chat participant entries must be objects")
        for entry in participant.get("commands", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ExtractionError(
                    "TODO: package.json chat participant commands must have literal names"
                )
            features.add(FeatureRef("extension_chat_participant_command", entry["name"]))
    if not features:
        raise ExtractionError("TODO: no VS Code extension commands found")
    return features


def extension_package_command_metadata() -> dict[str, PackageCommandMetadata]:
    payload = json.loads(EXTENSION_PACKAGE_PATH.read_text(encoding="utf-8"))
    contributes = payload.get("contributes")
    if not isinstance(contributes, dict):
        raise ExtractionError("TODO: package.json contributes object is missing")
    command_names = {
        entry["command"]
        for entry in contributes.get("commands", [])
        if isinstance(entry, dict) and isinstance(entry.get("command"), str)
    }
    hidden_from_palette: set[str] = set()
    menu_locations: dict[str, set[str]] = {command: set() for command in command_names}
    menus = contributes.get("menus", {})
    if menus is None:
        menus = {}
    if not isinstance(menus, dict):
        raise ExtractionError("TODO: package.json contributes.menus must be an object")
    for menu_id, entries in menus.items():
        if not isinstance(menu_id, str) or not isinstance(entries, list):
            raise ExtractionError("TODO: package.json menu contributions must be static arrays")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ExtractionError("TODO: package.json menu entries must be objects")
            command = entry.get("command")
            if not isinstance(command, str):
                continue
            if command not in command_names:
                continue
            if menu_id == "commandPalette" and entry.get("when") == "false":
                hidden_from_palette.add(command)
                continue
            menu_locations[command].add(menu_id)
    for command in command_names - hidden_from_palette:
        menu_locations[command].add("commandPalette")
    return {
        command: PackageCommandMetadata(
            hidden_from_command_palette=command in hidden_from_palette,
            menu_locations=tuple(sorted(locations)),
        )
        for command, locations in sorted(menu_locations.items())
    }


def _extension_command_registry_values() -> dict[str, str]:
    text = EXTENSION_COMMAND_REGISTRY_TS_PATH.read_text(encoding="utf-8")
    return dict(re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)\s*:\s*\"(alysis\.[^\"]+)\"", text))


def extract_registered_extension_command_names() -> set[str]:
    commands: set[str] = set()
    registry = _extension_command_registry_values()
    for path in sorted(EXTENSION_SRC_PATH.rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        commands.update(re.findall(r"\bregisterCommand\(\s*\"([^\"]+)\"", text))
        for key in re.findall(r"\bregisterCommand\(\s*COMMANDS\.([A-Za-z][A-Za-z0-9_]*)", text):
            command = registry.get(key)
            if command is None:
                raise ExtractionError(f"TODO: registerCommand references unknown COMMANDS.{key}")
            commands.add(command)
        commands.update(re.findall(r"\bcommandId\s*:\s*\"(alysis\.[^\"]+)\"", text))
    return commands


def extract_backend_action_group_ids() -> set[str]:
    text = EXTENSION_BACKEND_ACTION_METADATA_PATH.read_text(encoding="utf-8")
    start = text.find("BACKEND_ACTION_GROUPS")
    if start < 0:
        raise ExtractionError("TODO: BACKEND_ACTION_GROUPS is missing")
    end = text.find("] as const", start)
    if end < 0:
        raise ExtractionError("TODO: BACKEND_ACTION_GROUPS must remain a static array")
    block = text[start:end]
    groups = set(re.findall(r"\bid\s*:\s*\"([^\"]+)\"", block))
    if not groups:
        raise ExtractionError("TODO: no backend action groups inferred")
    return groups


def extract_backend_action_required_methods() -> dict[str, tuple[str, ...]]:
    text = EXTENSION_BACKEND_ACTION_METADATA_PATH.read_text(encoding="utf-8")
    actions: dict[str, tuple[str, ...]] = {}
    pattern = re.compile(
        r"\baction\(\s*"
        r"\"([^\"]+)\"\s*,\s*"
        r"\"[^\"]+\"\s*,\s*"
        r"\"[^\"]+\"\s*,\s*"
        r"\"[^\"]+\"\s*,\s*"
        r"\[[^\]]*\]\s*,\s*"
        r"\[([^\]]*)\]",
    )
    for line in text.splitlines():
        match = pattern.search(line)
        if match is None:
            continue
        action_id = match.group(1)
        methods = tuple(re.findall(r"\"([^\"]+)\"", match.group(2)))
        if not methods:
            raise ExtractionError(f"TODO: backend action {action_id!r} has no methods")
        actions[action_id] = methods
    if not actions:
        raise ExtractionError("TODO: no backend action metadata inferred")
    return actions


def _slash_registry_array_text(text: str) -> str:
    marker = "SLASH_COMMANDS"
    marker_index = text.find(marker)
    if marker_index < 0:
        raise ExtractionError("TODO: SLASH_COMMANDS registry is missing")
    assignment_index = text.find("=", marker_index)
    if assignment_index < 0:
        raise ExtractionError("TODO: SLASH_COMMANDS registry assignment is missing")
    start = text.find("[", assignment_index)
    if start < 0:
        raise ExtractionError("TODO: SLASH_COMMANDS registry is not a static array")
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ExtractionError("TODO: could not find the end of SLASH_COMMANDS")


def extract_extension_slash_features() -> set[FeatureRef]:
    text = EXTENSION_SLASH_REGISTRY_PATH.read_text(encoding="utf-8")
    array_text = _slash_registry_array_text(text)
    commands = re.findall(r"\bcommand\s*:\s*\"([^\"]+)\"", array_text)
    aliases: list[str] = []
    for alias_block in re.findall(r"\baliases\s*:\s*\[([^\]]*)\]", array_text, flags=re.S):
        aliases.extend(re.findall(r"\"([^\"]+)\"", alias_block))
    backend_text = EXTENSION_BACKEND_ACTION_METADATA_PATH.read_text(encoding="utf-8")
    backend_slash_commands = set(re.findall(r"\"(/[^\"]+)\"", backend_text))
    if not commands:
        raise ExtractionError("TODO: no extension slash commands found in SLASH_COMMANDS")
    return {
        *{FeatureRef("extension_slash_command", command) for command in commands},
        *{FeatureRef("extension_slash_command", command) for command in backend_slash_commands},
        *{FeatureRef("extension_slash_alias", alias) for alias in aliases},
    }


def extract_all_features() -> set[FeatureRef]:
    return {
        *extract_cli_command_features(),
        *extract_cli_chat_slash_features(),
        *extract_cli_forge_chat_features(),
        *extract_ide_method_features(),
        *extract_management_method_features(),
        *extract_extension_command_features(),
        *extract_extension_slash_features(),
    }


def load_parity_matrix(path: Path = PARITY_MATRIX_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parity_feature_refs(matrix: dict[str, Any]) -> set[FeatureRef]:
    return {
        FeatureRef(entry["surface"], entry["name"]) for entry in expanded_parity_entries(matrix)
    }


def parity_statuses_by_ref(matrix: dict[str, Any]) -> dict[FeatureRef, str]:
    statuses: dict[FeatureRef, str] = {}
    for entry in expanded_parity_entries(matrix):
        ref = FeatureRef(entry["surface"], entry["name"])
        status = str(entry["status"])
        previous = statuses.get(ref)
        if previous is not None and previous != status:
            raise ValueError(
                f"conflicting parity statuses for {ref.surface}: {ref.name} "
                f"({previous!r} vs {status!r})"
            )
        statuses[ref] = status
    return statuses


def parity_alias_groups(matrix: dict[str, Any]) -> set[CommandAliasGroup]:
    raw_aliases = matrix.get("aliases")
    if not isinstance(raw_aliases, list):
        raise ValueError("parity matrix must contain an aliases list")
    groups: set[CommandAliasGroup] = set()
    for index, entry in enumerate(raw_aliases):
        if not isinstance(entry, dict):
            raise ValueError(f"parity matrix alias {index} must be an object")
        surface = entry.get("surface")
        canonical = entry.get("canonical")
        aliases = entry.get("aliases")
        rationale = entry.get("rationale")
        if not isinstance(surface, str) or not surface:
            raise ValueError(f"parity matrix alias {index} must have surface")
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"parity matrix alias {index} must have canonical")
        if (
            not isinstance(aliases, list)
            or not aliases
            or not all(isinstance(alias, str) and alias for alias in aliases)
        ):
            raise ValueError(f"parity matrix alias {index} must have non-empty aliases")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"parity matrix alias {index} must have a rationale")
        groups.add(
            CommandAliasGroup(
                surface=surface,
                canonical=canonical,
                aliases=tuple(sorted(aliases)),
            )
        )
    return groups


def extension_command_routes(matrix: dict[str, Any]) -> dict[str, ExtensionCommandRoute]:
    raw_routes = matrix.get("extension_command_routes")
    if not isinstance(raw_routes, list):
        raise ValueError("parity matrix must contain an extension_command_routes list")
    routes: dict[str, ExtensionCommandRoute] = {}
    for index, entry in enumerate(raw_routes):
        if not isinstance(entry, dict):
            raise ValueError(f"extension command route {index} must be an object")
        command = entry.get("command")
        category = entry.get("category")
        route_type = entry.get("route_type")
        raw_backend_actions = entry.get("backend_actions", [])
        backend_action_group = entry.get("backend_action_group")
        handler = entry.get("handler")
        raw_menus = entry.get("menus", [])
        hidden_from_command_palette = entry.get("hidden_from_command_palette")
        rationale = entry.get("rationale")
        if not isinstance(command, str) or not command:
            raise ValueError(f"extension command route {index} must have command")
        if command in routes:
            raise ValueError(f"duplicate extension command route: {command}")
        if category not in VALID_EXTENSION_COMMAND_CATEGORIES:
            raise ValueError(
                f"extension command route {command} has invalid category: {category!r}"
            )
        if route_type not in VALID_EXTENSION_COMMAND_ROUTE_TYPES:
            raise ValueError(
                f"extension command route {command} has invalid route_type: {route_type!r}"
            )
        if not isinstance(raw_backend_actions, list) or not all(
            isinstance(action, str) and action for action in raw_backend_actions
        ):
            raise ValueError(f"extension command route {command} must list backend_actions")
        if backend_action_group is not None and not isinstance(backend_action_group, str):
            raise ValueError(
                f"extension command route {command} backend_action_group must be a string"
            )
        if handler is not None and not isinstance(handler, str):
            raise ValueError(f"extension command route {command} handler must be a string")
        if not isinstance(raw_menus, list) or not all(
            isinstance(menu, str) and menu for menu in raw_menus
        ):
            raise ValueError(f"extension command route {command} must list menus")
        if not isinstance(hidden_from_command_palette, bool):
            raise ValueError(
                f"extension command route {command} must set hidden_from_command_palette"
            )
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"extension command route {command} must have a rationale")
        backend_actions = tuple(sorted(raw_backend_actions))
        menus = tuple(sorted(raw_menus))
        if (
            route_type in {"backend_action", "context_backend_action_dispatch"}
            and not backend_actions
        ):
            raise ValueError(f"extension command route {command} must list backend actions")
        if route_type == "backend_action_group" and not backend_action_group:
            raise ValueError(f"extension command route {command} must name a backend action group")
        if route_type in {"registered_command_handler", "tree_refresh"} and not handler:
            raise ValueError(f"extension command route {command} must name a command handler")
        routes[command] = ExtensionCommandRoute(
            command=command,
            category=category,
            route_type=route_type,
            backend_actions=backend_actions,
            backend_action_group=backend_action_group,
            handler=handler,
            menus=menus,
            hidden_from_command_palette=hidden_from_command_palette,
            rationale=rationale,
        )
    return routes


def expanded_parity_entries(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    entries = matrix.get("entries")
    if not isinstance(entries, list):
        raise ValueError("parity matrix must contain an entries list")
    expanded: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"parity matrix entry {index} must be an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError(f"parity matrix entry {index} must have a non-empty id")
        if entry_id in seen_ids:
            raise ValueError(f"duplicate parity matrix id: {entry_id}")
        seen_ids.add(entry_id)
        surface = entry.get("surface")
        raw_name = entry.get("name")
        raw_names = entry.get("names")
        status = entry.get("status")
        rationale = entry.get("rationale")
        if not isinstance(surface, str):
            raise ValueError(f"parity matrix entry {entry_id} must have surface")
        if isinstance(raw_name, str):
            names = [raw_name]
        elif isinstance(raw_names, list) and all(isinstance(item, str) for item in raw_names):
            names = raw_names
        else:
            raise ValueError(f"parity matrix entry {entry_id} must have name or names")
        if status not in VALID_STATUSES:
            raise ValueError(f"parity matrix entry {entry_id} has invalid status: {status!r}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"parity matrix entry {entry_id} must have a rationale")
        if not names:
            raise ValueError(f"parity matrix entry {entry_id} must list at least one name")
        for name in names:
            expanded.append(
                {
                    **entry,
                    "name": name,
                    "names": None,
                }
            )
    return expanded


def _raw_entry_names(entry: dict[str, Any]) -> list[str]:
    raw_name = entry.get("name")
    raw_names = entry.get("names")
    if isinstance(raw_name, str):
        return [raw_name]
    if isinstance(raw_names, list) and all(isinstance(item, str) for item in raw_names):
        return raw_names
    raise ValueError(f"parity matrix entry {entry.get('id')!r} must have name or names")


def _markdown_list(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _metadata_line(entry: dict[str, Any], key: str, label: str) -> str | None:
    value = entry.get(key)
    if isinstance(value, str) and value.strip():
        return f"- {label}: {value.strip()}"
    if isinstance(value, list) and value:
        return f"- {label}: {_markdown_list(str(item) for item in value)}"
    if isinstance(value, dict) and value:
        parts = []
        for item_key in sorted(value):
            item_value = value[item_key]
            if isinstance(item_value, list):
                parts.append(f"`{item_key}` -> {_markdown_list(str(item) for item in item_value)}")
            else:
                parts.append(f"`{item_key}` -> `{item_value}`")
        return f"- {label}: " + "; ".join(parts)
    return None


def generate_parity_burndown(matrix: dict[str, Any]) -> str:
    raw_entries = matrix.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("parity matrix must contain an entries list")
    expanded = expanded_parity_entries(matrix)
    counts = Counter(str(entry["status"]) for entry in expanded)
    source_counts = {
        "CLI commands and groups": len(extract_cli_command_features()),
        "CLI chat slash commands": len(extract_cli_chat_slash_features()),
        "CLI Forge chat commands": len(extract_cli_forge_chat_features()),
        "IDE bridge methods": len(extract_ide_method_features()),
        "VS Code package/chat commands": len(extract_extension_command_features()),
        "VS Code slash commands and aliases": len(extract_extension_slash_features()),
        "Backend action metadata entries": len(extract_backend_action_required_methods()),
    }

    lines = [
        "# Alysis Code CLI to VS Code parity burn-down",
        "",
        "<!-- Generated by scripts/qa/check_ide_cli_parity.py. Do not edit by hand. -->",
        "",
        "This report is generated from `docs/generated/ide_cli_parity_matrix.json` and the current static feature inventory. It groups every tracked CLI, IDE bridge, VS Code command, backend action, and slash surface by final parity status.",
        "",
        "## Summary",
        "",
        "| Status | Feature count |",
        "| --- | ---: |",
    ]
    for status in sorted(VALID_STATUSES):
        lines.append(f"| `{status}` | {counts.get(status, 0)} |")

    lines.extend(["", "## Source Inventory", ""])
    for label, count in sorted(source_counts.items()):
        lines.append(f"- {label}: {count}")

    for status in sorted(VALID_STATUSES):
        entries = [
            entry
            for entry in raw_entries
            if isinstance(entry, dict) and entry.get("status") == status
        ]
        lines.extend(["", f"## {status}", ""])
        if not entries:
            lines.append("_No entries._")
            continue
        for entry in entries:
            names = _raw_entry_names(entry)
            lines.extend(
                [
                    f"### {entry['id']}",
                    "",
                    f"- Surface: `{entry['surface']}`",
                    f"- Features: {_markdown_list(names)}",
                    f"- Rationale: {entry['rationale'].strip()}",
                ]
            )
            for key, label in (
                ("owner", "Owner"),
                ("target_milestone", "Target milestone"),
                ("next_step", "Next step"),
                ("bridge_only_rationale", "Bridge-only rationale"),
                ("security_or_lifecycle_reason", "Security/lifecycle reason"),
                ("extension_equivalents", "Extension equivalents"),
            ):
                line = _metadata_line(entry, key, label)
                if line is not None:
                    lines.append(line)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _forge_specific_entry(entry: dict[str, Any]) -> bool:
    surface = str(entry.get("surface") or "")
    name = str(entry.get("name") or "")
    entry_id = str(entry.get("id") or "")
    if surface == "cli_forge_chat_command":
        return True
    if "forge" in entry_id or "swarm" in entry_id:
        return True
    lowered = name.lower()
    if "forge" in lowered or "swarm" in lowered:
        return True
    if surface == "ide_bridge_method" and (
        lowered.startswith("artifact.") or lowered.startswith("diff.")
    ):
        return True
    if surface == "extension_command" and (
        name.startswith("alysis.forge") or name == "alysis.manageForgeAssets"
    ):
        return True
    if surface in {"extension_slash_command", "extension_slash_alias"} and (
        lowered in {"/assets", "/artifacts", "/diffs", "/assistant", "/goal", "/task"}
        or lowered.startswith("/asset")
        or lowered.startswith("/execute")
        or lowered.startswith("/forge")
        or lowered.startswith("/plan")
        or lowered.startswith("/review")
    ):
        return True
    return False


def _display_status_for_forge(entry: dict[str, Any]) -> str:
    status = str(entry.get("status") or "")
    if status in {
        "implemented_in_extension",
        "intentionally_cli_only",
        "blocked_until_security_model",
        "experimental_with_gate",
    }:
        return status
    if status == "not_applicable_to_ide":
        return "intentionally_cli_only"
    return status


def generate_forge_parity_burndown(matrix: dict[str, Any]) -> str:
    expanded = [entry for entry in expanded_parity_entries(matrix) if _forge_specific_entry(entry)]
    counts = Counter(_display_status_for_forge(entry) for entry in expanded)
    ide_methods = sorted(ref.name for ref in extract_ide_method_features())
    swarm_methods = [method for method in ide_methods if method.startswith("forge.swarm")]

    lines = [
        "# Forge CLI to VS Code parity burn-down",
        "",
        "<!-- Generated by scripts/qa/check_ide_cli_parity.py. Do not edit by hand. -->",
        "",
        "This Forge-specific report is generated from `docs/generated/ide_cli_parity_matrix.json` and the current static feature inventory. It projects the Forge command, Forge chat, IDE bridge, VS Code command, and slash surfaces into a compact release-review table.",
        "",
        "## Summary",
        "",
        "| Forge status | Feature count |",
        "| --- | ---: |",
    ]
    for status in (
        "implemented_in_extension",
        "implemented_in_bridge_only",
        "intentionally_cli_only",
        "blocked_until_security_model",
        "experimental_with_gate",
    ):
        lines.append(f"| `{status}` | {counts.get(status, 0)} |")

    swarm_cli_status = next(
        (
            str(entry.get("status") or "")
            for entry in expanded
            if entry.get("surface") == "cli_command" and entry.get("name") == "forge swarm"
        ),
        "unknown",
    )
    lines.extend(
        [
            "",
            "## Swarm Gate",
            "",
            f"- `forge swarm` is `{swarm_cli_status}`.",
            (
                "- IDE bridge swarm job methods are advertised: cooperative checkpoint "
                "cancellation with preserved interrupted worktrees, a scheduler invariant "
                "guaranteeing disjoint concurrent write scopes, a swarm-layer write-scope "
                "dispatch guard, bounded redacted progress events with reconnect replay, "
                "and read-only reconcile with explicit harvest/discard actions."
                if swarm_methods
                else "- No `forge.swarm.*` IDE bridge methods are advertised."
            ),
            f"- Advertised swarm methods: {_markdown_list(swarm_methods) if swarm_methods else '_none_'}",
            "",
            "## Forge Features",
            "",
            "| Status | Surface | Feature | Rationale |",
            "| --- | --- | --- | --- |",
        ]
    )
    for entry in sorted(
        expanded,
        key=lambda item: (_display_status_for_forge(item), str(item["surface"]), str(item["name"])),
    ):
        lines.append(
            "| "
            f"`{_display_status_for_forge(entry)}` | "
            f"`{entry['surface']}` | "
            f"`{entry['name']}` | "
            f"{str(entry['rationale']).strip()} |"
        )

    return "\n".join(lines).rstrip() + "\n"


def _format_refs(refs: Iterable[FeatureRef]) -> str:
    return "\n".join(f"- {ref.surface}: {ref.name}" for ref in sorted(refs))


def _format_commands(commands: Iterable[str]) -> str:
    return "\n".join(f"- {command}" for command in sorted(commands))


def _format_alias_groups(groups: Iterable[CommandAliasGroup]) -> str:
    return "\n".join(
        f"- {group.surface}: {group.canonical} aliases {', '.join(group.aliases)}"
        for group in sorted(groups)
    )


def validate_parity_matrix() -> list[str]:
    errors: list[str] = []
    matrix = load_parity_matrix()
    declared_statuses = set(matrix.get("classification_enum") or [])
    if declared_statuses != VALID_STATUSES:
        errors.append(
            "classification_enum must match the stable status enum: "
            + ", ".join(sorted(VALID_STATUSES))
        )
    contract_tests = matrix.get("contract_tests")
    if (
        not isinstance(contract_tests, dict)
        or contract_tests.get("package_commands_route_metadata_required") is not True
    ):
        errors.append("contract_tests.package_commands_route_metadata_required must be true")
    for key in (
        "planned_protocol_metadata_required",
        "blocked_security_reason_required",
        "bridge_only_rationale_required",
        "burn_down_report_required",
        "forge_burn_down_report_required",
    ):
        if not isinstance(contract_tests, dict) or contract_tests.get(key) is not True:
            errors.append(f"contract_tests.{key} must be true")

    metadata_errors: list[str] = []
    for entry in matrix.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id"))
        status = entry.get("status")
        if status == "planned_for_protocol":
            for key in ("owner", "target_milestone", "next_step"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    metadata_errors.append(f"{entry_id}: planned_for_protocol requires {key}")
        elif status == "blocked_until_security_model":
            reason = entry.get("security_or_lifecycle_reason")
            if not isinstance(reason, str) or not reason.strip():
                metadata_errors.append(
                    f"{entry_id}: blocked_until_security_model requires "
                    "security_or_lifecycle_reason"
                )
        elif status == "implemented_in_bridge_only":
            reason = entry.get("bridge_only_rationale")
            if not isinstance(reason, str) or not reason.strip():
                metadata_errors.append(
                    f"{entry_id}: implemented_in_bridge_only requires bridge_only_rationale"
                )
    if metadata_errors:
        errors.append(
            "Parity entries with unresolved or bridge-only statuses must carry "
            "explicit governance metadata:\n" + "\n".join(f"- {error}" for error in metadata_errors)
        )

    if PARITY_BURNDOWN_PATH.exists():
        expected_burndown = generate_parity_burndown(matrix)
        actual_burndown = PARITY_BURNDOWN_PATH.read_text(encoding="utf-8")
        if actual_burndown != expected_burndown:
            errors.append(
                "docs/generated/ide_cli_parity_burndown.md is out of date; "
                "run scripts/qa/check_ide_cli_parity.py --write-burndown"
            )
    else:
        errors.append(
            "docs/generated/ide_cli_parity_burndown.md is missing; "
            "run scripts/qa/check_ide_cli_parity.py --write-burndown"
        )
    if FORGE_PARITY_BURNDOWN_PATH.exists():
        expected_forge_burndown = generate_forge_parity_burndown(matrix)
        actual_forge_burndown = FORGE_PARITY_BURNDOWN_PATH.read_text(encoding="utf-8")
        if actual_forge_burndown != expected_forge_burndown:
            errors.append(
                "docs/generated/ide_forge_parity_burndown.md is out of date; "
                "run scripts/qa/check_ide_cli_parity.py --write-burndown"
            )
    else:
        errors.append(
            "docs/generated/ide_forge_parity_burndown.md is missing; "
            "run scripts/qa/check_ide_cli_parity.py --write-burndown"
        )

    matrix_refs = parity_feature_refs(matrix)
    matrix_statuses = parity_statuses_by_ref(matrix)
    forge_review_statuses = {
        "implemented_in_extension",
        "intentionally_cli_only",
        "blocked_until_security_model",
        "experimental_with_gate",
    }
    for entry in expanded_parity_entries(matrix):
        if not _forge_specific_entry(entry):
            continue
        status = _display_status_for_forge(entry)
        if status == "implemented_in_bridge_only":
            # Allowed for Forge features only as an explicit transitional state:
            # the bridge ships the method first and the extension adopts it in a
            # tracked follow-up. The written bridge_only_rationale keeps the
            # burndown reviewable.
            if not str(entry.get("bridge_only_rationale") or "").strip():
                errors.append(
                    "Forge parity feature "
                    f"{entry['surface']} {entry['name']} is implemented_in_bridge_only "
                    "but is missing a bridge_only_rationale explaining the planned "
                    "extension adoption"
                )
            continue
        if status not in forge_review_statuses:
            errors.append(
                "Forge parity feature "
                f"{entry['surface']} {entry['name']} projects to invalid Forge review status "
                f"{status!r}; use implemented_in_extension, intentionally_cli_only, "
                "blocked_until_security_model, experimental_with_gate, or "
                "implemented_in_bridge_only with a bridge_only_rationale"
            )

    extracted_refs = extract_all_features()
    missing = extracted_refs - matrix_refs
    if missing:
        errors.append(
            "The parity matrix is missing extracted features. Add entries with rationale:\n"
            + _format_refs(missing)
        )

    detected_alias_groups = extract_cli_command_alias_groups()
    declared_alias_groups = parity_alias_groups(matrix)
    missing_alias_groups = detected_alias_groups - declared_alias_groups
    if missing_alias_groups:
        errors.append(
            "The parity matrix is missing Typer command alias metadata:\n"
            + _format_alias_groups(missing_alias_groups)
        )

    for alias_group in sorted(declared_alias_groups):
        canonical_ref = FeatureRef(alias_group.surface, alias_group.canonical)
        alias_refs = [FeatureRef(alias_group.surface, alias) for alias in alias_group.aliases]
        missing_alias_refs = [ref for ref in [canonical_ref, *alias_refs] if ref not in matrix_refs]
        if missing_alias_refs:
            errors.append(
                "Parity alias metadata references entries absent from the matrix:\n"
                + _format_refs(missing_alias_refs)
            )
            continue
        statuses = {
            matrix_statuses[ref] for ref in [canonical_ref, *alias_refs] if ref in matrix_statuses
        }
        if len(statuses) > 1:
            errors.append(
                "Parity aliases must share a compatible status unless split with a "
                "separate behavioral-difference rationale:\n" + _format_alias_groups([alias_group])
            )

    management_methods = extract_management_method_features()
    management_handlers = extract_management_handler_features()
    missing_handlers = management_methods - management_handlers
    orphan_handlers = management_handlers - management_methods
    if missing_handlers:
        errors.append(
            "management_protocol.py _HANDLERS is missing MANAGEMENT_METHODS entries:\n"
            + _format_refs(missing_handlers)
        )
    if orphan_handlers:
        errors.append(
            "management_protocol.py _HANDLERS has entries absent from MANAGEMENT_METHODS:\n"
            + _format_refs(orphan_handlers)
        )

    health_methods = extract_ide_method_features()
    missing_management_health = management_methods - health_methods
    if missing_management_health:
        errors.append(
            "health.py SUPPORTED_METHODS is missing management methods:\n"
            + _format_refs(missing_management_health)
        )

    dispatch_methods = extract_stdio_dispatch_method_features()
    missing_dispatch_methods = health_methods - dispatch_methods
    if missing_dispatch_methods:
        errors.append(
            "health.py advertises IDE bridge methods without a stdio dispatch handler "
            "or management routing:\n" + _format_refs(missing_dispatch_methods)
        )

    ide_doc = IDE_PROTOCOL_DOC_PATH.read_text(encoding="utf-8")
    missing_protocol_methods = [
        ref.name for ref in sorted(extract_ide_method_features()) if ref.name not in ide_doc
    ]
    if missing_protocol_methods:
        errors.append(
            "docs/ide_protocol.md is missing IDE bridge methods: "
            + ", ".join(missing_protocol_methods)
        )

    ts_protocol_methods = extract_ts_protocol_method_features()
    extension_methods = {ref for ref in health_methods if ref.name not in DESKTOP_HOST_ONLY_METHODS}
    missing_ts_protocol_methods = extension_methods - ts_protocol_methods
    if missing_ts_protocol_methods:
        errors.append(
            "AlysisProtocol.ts is missing advertised IDE bridge methods:\n"
            + _format_refs(missing_ts_protocol_methods)
        )

    ts_client_methods = extract_ts_bridge_client_method_features()
    client_required_methods = {
        ref for ref in extension_methods if ref.name not in TS_CLIENT_COVERAGE_EXEMPT_METHODS
    }
    missing_ts_client_methods = client_required_methods - ts_client_methods
    if missing_ts_client_methods:
        errors.append(
            "AlysisBridgeClient.ts is missing typed request coverage for "
            "advertised IDE bridge methods:\n" + _format_refs(missing_ts_client_methods)
        )

    protocol_contract_methods = extract_protocol_contract_method_features()
    missing_protocol_contract_methods = health_methods - protocol_contract_methods
    extra_protocol_contract_methods = protocol_contract_methods - health_methods
    if missing_protocol_contract_methods:
        errors.append(
            "docs/generated/ide_protocol_methods.json is missing advertised methods:\n"
            + _format_refs(missing_protocol_contract_methods)
        )
    if extra_protocol_contract_methods:
        errors.append(
            "docs/generated/ide_protocol_methods.json contains unadvertised methods:\n"
            + _format_refs(extra_protocol_contract_methods)
        )

    backend_action_methods = extract_backend_action_method_features()
    cockpit_route_methods = {
        FeatureRef("ide_bridge_method", method) for method in COCKPIT_ROUTE_METHODS
    }
    routed_ide_methods = backend_action_methods | cockpit_route_methods
    implemented_ide_methods = {
        ref
        for ref, status in matrix_statuses.items()
        if ref.surface == "ide_bridge_method" and status == "implemented_in_extension"
    }
    missing_user_routes = implemented_ide_methods - routed_ide_methods
    if missing_user_routes:
        errors.append(
            "IDE methods marked implemented_in_extension must be reachable through "
            "backend action metadata or documented Cockpit/controller routes:\n"
            + _format_refs(missing_user_routes)
        )
    bridge_only_routed = {
        ref
        for ref, status in matrix_statuses.items()
        if ref.surface == "ide_bridge_method"
        and status == "implemented_in_bridge_only"
        and ref in routed_ide_methods
    }
    if bridge_only_routed:
        errors.append(
            "IDE methods marked implemented_in_bridge_only must not be advertised through "
            "extension user-facing routes:\n" + _format_refs(bridge_only_routed)
        )

    extracted_extension_routes = (
        extract_extension_command_features() | extract_extension_slash_features()
    )
    declared_extension_routes = {
        ref
        for ref, status in matrix_statuses.items()
        if ref.surface.startswith("extension_") and status == "implemented_in_extension"
    }
    missing_extracted_extension_routes = declared_extension_routes - extracted_extension_routes
    if missing_extracted_extension_routes:
        errors.append(
            "Extension routes marked implemented_in_extension are not contributed or registered:\n"
            + _format_refs(missing_extracted_extension_routes)
        )

    extension_slash_names = {ref.name for ref in extract_extension_slash_features()}
    implemented_cli_slash_route_errors: list[str] = []
    for entry in matrix.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "implemented_in_extension":
            continue
        if entry.get("surface") not in {
            "cli_chat_slash_command",
            "cli_forge_chat_command",
        }:
            continue
        equivalents = entry.get("extension_equivalents", {})
        if equivalents is not None and not isinstance(equivalents, dict):
            implemented_cli_slash_route_errors.append(
                f"{entry.get('id')}: extension_equivalents must be an object"
            )
            continue
        for name in _raw_entry_names(entry):
            if name in extension_slash_names:
                continue
            raw_equivalents = equivalents.get(name) if isinstance(equivalents, dict) else None
            if isinstance(raw_equivalents, str):
                candidate_routes = [raw_equivalents]
            elif isinstance(raw_equivalents, list) and all(
                isinstance(item, str) for item in raw_equivalents
            ):
                candidate_routes = raw_equivalents
            else:
                candidate_routes = []
            if any(candidate in extension_slash_names for candidate in candidate_routes):
                continue
            implemented_cli_slash_route_errors.append(
                f"{entry.get('id')}: {name} lacks an exact extension slash route "
                "or documented extension_equivalents route"
            )
    if implemented_cli_slash_route_errors:
        errors.append(
            "CLI slash/Forge chat commands marked implemented_in_extension must "
            "have a reachable extension slash route:\n"
            + "\n".join(f"- {error}" for error in implemented_cli_slash_route_errors)
        )

    package_command_metadata = extension_package_command_metadata()
    package_commands = set(package_command_metadata)
    command_routes = extension_command_routes(matrix)
    route_commands = set(command_routes)
    missing_command_route_metadata = package_commands - route_commands
    if missing_command_route_metadata:
        errors.append(
            "The parity matrix is missing extension_command_routes metadata for package commands:\n"
            + _format_commands(missing_command_route_metadata)
        )
    unknown_command_route_metadata = route_commands - package_commands
    if unknown_command_route_metadata:
        errors.append(
            "extension_command_routes references commands absent from package.json:\n"
            + _format_commands(unknown_command_route_metadata)
        )

    registered_extension_commands = extract_registered_extension_command_names()
    implemented_extension_commands = {
        ref.name
        for ref, status in matrix_statuses.items()
        if ref.surface == "extension_command" and status == "implemented_in_extension"
    }
    implemented_without_registered_handler = (
        implemented_extension_commands - registered_extension_commands
    )
    if implemented_without_registered_handler:
        errors.append(
            "Extension commands marked implemented_in_extension must have a reachable "
            "registered command handler:\n"
            + _format_commands(implemented_without_registered_handler)
        )
    implemented_without_route_metadata = implemented_extension_commands - route_commands
    if implemented_without_route_metadata:
        errors.append(
            "Extension commands marked implemented_in_extension must have route metadata:\n"
            + _format_commands(implemented_without_route_metadata)
        )

    backend_action_methods_by_id = extract_backend_action_required_methods()
    backend_action_group_ids = extract_backend_action_group_ids()
    route_errors: list[str] = []
    for route in sorted(command_routes.values(), key=lambda item: item.command):
        package_metadata = package_command_metadata.get(route.command)
        if package_metadata is not None:
            declared_menus = set(route.menus)
            actual_menus = set(package_metadata.menu_locations)
            if route.hidden_from_command_palette != package_metadata.hidden_from_command_palette:
                route_errors.append(
                    f"{route.command}: hidden_from_command_palette must match package.json"
                )
            if not declared_menus <= actual_menus:
                extra = ", ".join(sorted(declared_menus - actual_menus))
                route_errors.append(f"{route.command}: route menus are not contributed: {extra}")
            if route.category == "command_palette" and "commandPalette" not in actual_menus:
                route_errors.append(f"{route.command}: command_palette route is hidden")
            if route.category in {"session_tree_context", "manage_tree_context"}:
                if "view/item/context" not in actual_menus:
                    route_errors.append(
                        f"{route.command}: tree context command must be in view/item/context"
                    )
                if not route.hidden_from_command_palette:
                    route_errors.append(
                        f"{route.command}: tree context command must be hidden from command palette"
                    )
            if route.category == "manage_tree_title" and "view/title" not in actual_menus:
                route_errors.append(
                    f"{route.command}: manage tree title command must be in view/title"
                )
            if route.category == "cockpit_programmatic":
                # Programmatic routes have no user-visible menu surface: the only acceptable menu
                # contribution is the suppression entry that keeps them out of the palette.
                if not route.hidden_from_command_palette:
                    route_errors.append(
                        f"{route.command}: cockpit programmatic command must be hidden from command palette"
                    )
                if actual_menus - {"commandPalette"}:
                    extra = ", ".join(sorted(actual_menus - {"commandPalette"}))
                    route_errors.append(
                        f"{route.command}: cockpit programmatic command must not appear in menus: {extra}"
                    )
        if route.route_type == "backend_action_group":
            if route.backend_action_group not in backend_action_group_ids:
                route_errors.append(
                    f"{route.command}: unknown backend action group {route.backend_action_group!r}"
                )
        if route.backend_actions:
            for action_id in route.backend_actions:
                required_methods = backend_action_methods_by_id.get(action_id)
                if required_methods is None:
                    route_errors.append(f"{route.command}: unknown backend action {action_id!r}")
                    continue
                command_status = matrix_statuses.get(FeatureRef("extension_command", route.command))
                if command_status != "implemented_in_extension":
                    continue
                blocked_methods = [
                    method
                    for method in required_methods
                    if matrix_statuses.get(FeatureRef("ide_bridge_method", method))
                    != "implemented_in_extension"
                ]
                if blocked_methods:
                    route_errors.append(
                        f"{route.command}: implemented extension command routes to "
                        f"non-implemented IDE methods: {', '.join(blocked_methods)}"
                    )
    if route_errors:
        errors.append(
            "extension_command_routes has invalid or unreachable route metadata:\n"
            + "\n".join(f"- {error}" for error in route_errors)
        )

    vscode_doc = VSCODE_EXTENSION_DOC_PATH.read_text(encoding="utf-8")
    missing_extension_slash_docs = [
        ref.name for ref in sorted(extract_extension_slash_features()) if ref.name not in vscode_doc
    ]
    if missing_extension_slash_docs:
        errors.append(
            "docs/vscode_extension.md is missing extension slash commands: "
            + ", ".join(missing_extension_slash_docs)
        )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-extracted",
        action="store_true",
        help="Print the statically extracted feature references as JSON.",
    )
    parser.add_argument(
        "--write-burndown",
        action="store_true",
        help="Regenerate docs/generated/ide_cli_parity_burndown.md.",
    )
    args = parser.parse_args(argv)

    if args.list_extracted:
        print(
            json.dumps(
                [
                    {"surface": ref.surface, "name": ref.name}
                    for ref in sorted(extract_all_features())
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.write_burndown:
        matrix = load_parity_matrix()
        PARITY_BURNDOWN_PATH.write_text(
            generate_parity_burndown(matrix),
            encoding="utf-8",
        )
        FORGE_PARITY_BURNDOWN_PATH.write_text(
            generate_forge_parity_burndown(matrix),
            encoding="utf-8",
        )
        print(f"Wrote {PARITY_BURNDOWN_PATH.relative_to(REPO_ROOT)}.")
        print(f"Wrote {FORGE_PARITY_BURNDOWN_PATH.relative_to(REPO_ROOT)}.")
        return 0

    errors = validate_parity_matrix()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("IDE/CLI parity matrix is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
