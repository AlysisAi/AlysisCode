from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.qa import check_ide_cli_parity as parity  # noqa: E402


def _matrix_status(surface: str, name: str) -> str:
    matrix = parity.load_parity_matrix()
    for entry in parity.expanded_parity_entries(matrix):
        if entry["surface"] == surface and entry["name"] == name:
            return str(entry["status"])
    raise AssertionError(f"Missing parity entry for {surface}: {name}")


def _matrix_entry(surface: str, name: str) -> dict[str, object]:
    matrix = parity.load_parity_matrix()
    for entry in parity.expanded_parity_entries(matrix):
        if entry["surface"] == surface and entry["name"] == name:
            return entry
    raise AssertionError(f"Missing parity entry for {surface}: {name}")


def test_parity_extraction_finds_current_command_surfaces() -> None:
    features = parity.extract_all_features()

    assert parity.FeatureRef("cli_command", "run") in features
    assert parity.FeatureRef("cli_command", "chat") in features
    assert parity.FeatureRef("cli_command", "forge assets add") in features
    assert parity.FeatureRef("cli_command_group", "forge assets") in features
    assert parity.FeatureRef("cli_chat_slash_command", "/forge") in features
    assert parity.FeatureRef("cli_chat_slash_command", "/subagents") in features
    assert parity.FeatureRef("cli_forge_chat_command", "/execute") in features
    assert parity.FeatureRef("extension_command", "alysis.forgeExecute") in features
    assert parity.FeatureRef("extension_slash_command", "/forge plan") in features
    assert parity.FeatureRef("ide_bridge_method", "forge.executePreview") in features


def test_cli_extraction_follows_nested_imports_and_scopes_app_names(tmp_path, monkeypatch):
    root = tmp_path / "root.py"
    root.write_text(
        'import typer\nfrom .child import app as child_app\napp = typer.Typer()\napp.add_typer(child_app, name="first")\n'
    )
    (tmp_path / "child.py").write_text(
        'import typer\nfrom .leaf import app as leaf_app\napp = typer.Typer()\napp.add_typer(leaf_app, name="second")\n'
    )
    (tmp_path / "leaf.py").write_text(
        "import typer\napp = typer.Typer()\n@app.command()\ndef execute(): pass\n"
    )
    monkeypatch.setattr(parity, "CLI_ROOT_PATH", root)
    assert (
        parity.FeatureRef("cli_command", "first second execute")
        in parity.extract_cli_command_features()
    )


def test_parity_validation_script_passes_for_current_repo() -> None:
    assert parity.validate_parity_matrix() == []


def test_parity_burndown_report_is_current() -> None:
    matrix = parity.load_parity_matrix()
    burndown = REPO_ROOT / "docs" / "generated" / "ide_cli_parity_burndown.md"

    assert burndown.read_text(encoding="utf-8") == parity.generate_parity_burndown(matrix)


def test_forge_parity_burndown_report_is_current() -> None:
    matrix = parity.load_parity_matrix()
    burndown = REPO_ROOT / "docs" / "generated" / "ide_forge_parity_burndown.md"

    assert burndown.read_text(encoding="utf-8") == parity.generate_forge_parity_burndown(matrix)


def test_forge_swarm_bridge_methods_are_advertised_with_lifecycle_guarantees() -> None:
    # SW6: the swarm gate fully lifted into the IDE. The cockpit Run Swarm
    # console routes the swarm job + review methods, so forge swarm is
    # implemented_in_extension and chat /execute is experimental_with_gate
    # (review-only, capability-gated; broad auto/fullaccess stays blocked).
    # Durable list/resume are now routed by the native recovery picker;
    # forge.swarm.reconcile stays bridge-only as an internal crash-recovery primitive.
    matrix = parity.load_parity_matrix()
    forge_report = parity.generate_forge_parity_burndown(matrix)
    ide_methods = {ref.name for ref in parity.extract_ide_method_features()}

    assert _matrix_status("cli_command", "forge swarm") == "implemented_in_extension"
    assert _matrix_status("cli_forge_chat_command", "/execute") == "experimental_with_gate"
    expected_swarm_methods = {
        "forge.swarm.start",
        "forge.swarm.resume",
        "forge.swarm.list",
        "forge.swarm.status",
        "forge.swarm.result",
        "forge.swarm.cancel",
        "forge.swarm.reconcile",
        "forge.swarm.review",
        "forge.swarm.apply",
        "forge.swarm.discard",
    }
    assert {
        method for method in ide_methods if method.startswith("forge.swarm")
    } == expected_swarm_methods
    assert "`forge swarm` is `implemented_in_extension`." in forge_report
    assert "Advertised swarm methods:" in forge_report
    assert "`forge.swarm.reconcile`" in forge_report
    assert "`forge.swarm.resume`" in forge_report
    assert "`forge.swarm.list`" in forge_report
    assert _matrix_status("ide_bridge_method", "forge.swarm.resume") == "implemented_in_extension"
    assert _matrix_status("ide_bridge_method", "forge.swarm.list") == "implemented_in_extension"
    # /execute is now experimental_with_gate (the capability-gated Run Swarm path).
    assert "| `experimental_with_gate` | 1 |" in forge_report


def test_forge_asset_slash_routes_are_in_forge_burndown() -> None:
    matrix = parity.load_parity_matrix()
    forge_report = parity.generate_forge_parity_burndown(matrix)

    for alias in ("/asset edit", "/asset cancel-pending", "/asset prune"):
        assert f"`{alias}`" in forge_report


def test_managed_browser_methods_are_routed_by_the_native_cockpit() -> None:
    expected = {
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
    }
    ide_methods = {ref.name for ref in parity.extract_ide_method_features()}

    assert expected <= ide_methods
    for method in expected:
        entry = _matrix_entry("ide_bridge_method", method)
        assert entry["status"] == "implemented_in_extension"
        assert "native Browser Cockpit" in str(entry.get("rationale") or "")


def test_forge_parity_entries_project_to_release_review_statuses() -> None:
    matrix = parity.load_parity_matrix()
    allowed = {
        "implemented_in_extension",
        "intentionally_cli_only",
        "blocked_until_security_model",
        "experimental_with_gate",
    }

    for entry in parity.expanded_parity_entries(matrix):
        if not parity._forge_specific_entry(entry):  # noqa: SLF001 - test covers generator policy.
            continue
        status = parity._display_status_for_forge(entry)  # noqa: SLF001 - test covers generator policy.
        if status == "implemented_in_bridge_only":
            # Transitional state for bridge-first Forge methods: it must carry a
            # written rationale describing the planned extension adoption.
            assert str(entry.get("bridge_only_rationale") or "").strip(), (
                f"{entry['id']} is implemented_in_bridge_only without a bridge_only_rationale"
            )
            continue
        assert status in allowed, f"{entry['id']} has unexpected Forge status {status!r}"


def test_no_feature_is_left_in_ambiguous_parity_status() -> None:
    matrix = parity.load_parity_matrix()
    errors: list[str] = []

    for entry in matrix["entries"]:
        status = entry["status"]
        if status == "planned_for_protocol":
            for key in ("owner", "target_milestone", "next_step"):
                if not entry.get(key):
                    errors.append(f"{entry['id']} missing {key}")
        elif status == "blocked_until_security_model" and not entry.get(
            "security_or_lifecycle_reason"
        ):
            errors.append(f"{entry['id']} missing security_or_lifecycle_reason")
        elif status == "implemented_in_bridge_only" and not entry.get("bridge_only_rationale"):
            errors.append(f"{entry['id']} missing bridge_only_rationale")

    assert errors == []


def test_current_parity_burndown_has_no_planned_protocol_work() -> None:
    matrix = parity.load_parity_matrix()
    planned = [
        entry["id"] for entry in matrix["entries"] if entry["status"] == "planned_for_protocol"
    ]
    burndown = REPO_ROOT / "docs" / "generated" / "ide_cli_parity_burndown.md"
    text = burndown.read_text(encoding="utf-8")

    assert planned == []
    assert "| `planned_for_protocol` | 0 |" in text


def test_implemented_cli_slash_entries_have_reachable_extension_routes() -> None:
    matrix = parity.load_parity_matrix()
    extension_slash = {ref.name for ref in parity.extract_extension_slash_features()}
    missing: list[str] = []

    for entry in matrix["entries"]:
        if entry["status"] != "implemented_in_extension":
            continue
        if entry["surface"] not in {"cli_chat_slash_command", "cli_forge_chat_command"}:
            continue
        names = [entry["name"]] if "name" in entry else entry["names"]
        equivalents = entry.get("extension_equivalents", {})
        for name in names:
            if name in extension_slash:
                continue
            candidates = equivalents.get(name, [])
            if isinstance(candidates, str):
                candidates = [candidates]
            if not any(candidate in extension_slash for candidate in candidates):
                missing.append(f"{entry['id']}:{name}")

    assert missing == []


def test_intentionally_blocked_features_are_not_advertised_as_supported() -> None:
    extension_slash = {ref.name for ref in parity.extract_extension_slash_features()}

    assert "/trace" in extension_slash
    assert "/terminals" in extension_slash
    assert "/paste-image" in extension_slash
    assert "/model-info" in extension_slash
    assert "/subagents" in extension_slash
    assert "/subagent" not in extension_slash
    assert "/permissions" in extension_slash
    assert "/mode" not in extension_slash
    assert "/plan" not in extension_slash
    assert _matrix_status("cli_chat_slash_command", "/trace") == "implemented_in_extension"
    assert _matrix_status("cli_chat_slash_command", "/terminals") == "implemented_in_extension"
    assert _matrix_status("cli_chat_slash_command", "/trace raw") == "blocked_until_security_model"
    assert (
        _matrix_status("cli_chat_slash_command", "/terminals start")
        == "blocked_until_security_model"
    )
    assert (
        _matrix_status("cli_chat_slash_command", "/terminals interactive")
        == "blocked_until_security_model"
    )
    assert _matrix_status("cli_chat_slash_command", "/paste-image") == "implemented_in_extension"
    assert _matrix_status("cli_chat_slash_command", "/model-info") == "implemented_in_extension"
    assert _matrix_status("cli_chat_slash_command", "/subagents") == "not_applicable_to_ide"
    assert (
        _matrix_status("extension_command", "alysis.manage.mcpLogin") == "implemented_in_extension"
    )


def test_current_ide_bridge_methods_are_represented_in_protocol_docs() -> None:
    doc = (REPO_ROOT / "docs" / "ide_protocol.md").read_text(encoding="utf-8")

    missing = [
        feature.name
        for feature in sorted(parity.extract_ide_method_features())
        if feature.name not in doc
    ]

    assert missing == []


def test_management_methods_have_handlers_health_and_parity_entries() -> None:
    management_methods = parity.extract_management_method_features()
    management_handlers = parity.extract_management_handler_features()
    health_methods = parity.extract_ide_method_features()
    matrix_refs = parity.parity_feature_refs(parity.load_parity_matrix())

    assert management_methods == management_handlers
    assert management_methods <= health_methods
    assert management_methods <= matrix_refs


def test_cli_command_aliases_are_extracted_and_declared() -> None:
    detected = parity.extract_cli_command_alias_groups()
    declared = parity.parity_alias_groups(parity.load_parity_matrix())

    skill_init_alias = parity.CommandAliasGroup(
        surface="cli_command",
        canonical="skill init",
        aliases=("skill create",),
    )
    skill_remove_alias = parity.CommandAliasGroup(
        surface="cli_command",
        canonical="skill remove",
        aliases=("skill uninstall",),
    )

    assert skill_init_alias in detected
    assert skill_remove_alias in detected
    assert detected <= declared


def test_skill_cli_aliases_share_canonical_bridge_classification() -> None:
    assert _matrix_status("cli_command", "skill init") == "implemented_in_extension"
    assert _matrix_status("cli_command", "skill create") == "implemented_in_extension"
    assert _matrix_status("cli_command", "skill remove") == "implemented_in_extension"
    assert _matrix_status("cli_command", "skill uninstall") == "implemented_in_extension"


def test_advertised_ide_methods_have_dispatch_docs_protocol_and_client_coverage() -> None:
    health_methods = parity.extract_ide_method_features()
    dispatch_methods = parity.extract_stdio_dispatch_method_features()
    ts_protocol_methods = parity.extract_ts_protocol_method_features()
    ts_client_methods = parity.extract_ts_bridge_client_method_features()
    protocol_contract_methods = parity.extract_protocol_contract_method_features()
    extension_methods = {
        ref for ref in health_methods if ref.name not in parity.DESKTOP_HOST_ONLY_METHODS
    }
    client_required_methods = {
        ref for ref in extension_methods if ref.name not in parity.TS_CLIENT_COVERAGE_EXEMPT_METHODS
    }

    assert health_methods <= dispatch_methods
    assert extension_methods <= ts_protocol_methods
    assert health_methods == protocol_contract_methods
    assert client_required_methods <= ts_client_methods


def test_vscode_package_commands_are_represented_in_parity_matrix() -> None:
    matrix = parity.load_parity_matrix()
    matrix_refs = parity.parity_feature_refs(matrix)
    command_routes = parity.extension_command_routes(matrix)
    package_commands = {
        feature
        for feature in parity.extract_extension_command_features()
        if feature.surface == "extension_command"
    }

    assert package_commands
    assert package_commands <= matrix_refs
    assert {feature.name for feature in package_commands} == set(command_routes)

    new_session_route = command_routes["alysis.newSession"]
    assert _matrix_status("extension_command", "alysis.newSession") == "implemented_in_extension"
    assert new_session_route.route_type == "registered_command_handler"
    assert new_session_route.handler == "commands/openChat.ts: chatController.newSession"
    assert set(new_session_route.menus) == {"commandPalette", "view/title"}
    assert new_session_route.hidden_from_command_palette is False


def test_hidden_commands_map_to_valid_backend_actions_or_handlers() -> None:
    matrix = parity.load_parity_matrix()
    routes = parity.extension_command_routes(matrix)
    package_metadata = parity.extension_package_command_metadata()
    backend_actions = parity.extract_backend_action_required_methods()
    registered_commands = parity.extract_registered_extension_command_names()

    hidden_context_commands = {
        command
        for command, metadata in package_metadata.items()
        if metadata.hidden_from_command_palette
    }

    assert hidden_context_commands
    for command in hidden_context_commands:
        route = routes[command]
        assert route.category in {
            "session_tree_context",
            "manage_tree_context",
            "cockpit_programmatic",
        }, command
        assert route.hidden_from_command_palette is True
        assert route.route_type in {
            "backend_action",
            "context_backend_action_dispatch",
            "registered_command_handler",
            "tree_refresh",
        }, command
        assert command in registered_commands
        if route.backend_actions:
            for action_id in route.backend_actions:
                assert action_id in backend_actions, f"{command} -> {action_id}"
        else:
            assert route.handler, command


def test_extension_commands_marked_implemented_have_reachable_routes() -> None:
    matrix = parity.load_parity_matrix()
    routes = parity.extension_command_routes(matrix)
    registered_commands = parity.extract_registered_extension_command_names()
    backend_actions = parity.extract_backend_action_required_methods()
    backend_groups = parity.extract_backend_action_group_ids()

    implemented_commands = {
        ref.name
        for ref, status in parity.parity_statuses_by_ref(matrix).items()
        if ref.surface == "extension_command" and status == "implemented_in_extension"
    }

    assert implemented_commands
    for command in implemented_commands:
        route = routes[command]
        assert command in registered_commands
        if route.route_type == "backend_action_group":
            assert route.backend_action_group in backend_groups, command
        if route.backend_actions:
            assert set(route.backend_actions) <= set(backend_actions), command


def test_extension_command_route_categories_cover_palette_and_current_cockpit_surfaces() -> None:
    routes = parity.extension_command_routes(parity.load_parity_matrix())
    categories = {route.category for route in routes.values()}

    assert "command_palette" in categories
    assert "cockpit_programmatic" in categories
    assert routes["alysis.session.resume"].category == "cockpit_programmatic"
    assert routes["alysis.manage.mcpLogin"].category == "cockpit_programmatic"
    assert (
        _matrix_status("extension_command", "alysis.manage.mcpLogin") == "implemented_in_extension"
    )


def test_extension_slash_commands_are_documented_and_classified() -> None:
    matrix_refs = parity.parity_feature_refs(parity.load_parity_matrix())
    vscode_doc = (REPO_ROOT / "docs" / "vscode_extension.md").read_text(encoding="utf-8")
    extension_slash = parity.extract_extension_slash_features()

    assert extension_slash
    assert extension_slash <= matrix_refs
    assert [
        feature.name for feature in sorted(extension_slash) if feature.name not in vscode_doc
    ] == []


def test_known_intentionally_excluded_commands_have_correct_classification() -> None:
    assert _matrix_status("cli_command", "server start") == "intentionally_cli_only"
    assert _matrix_status("cli_command", "config menu") == "intentionally_cli_only"
    assert _matrix_status("cli_command", "config set-api-key") == "intentionally_cli_only"
    assert _matrix_status("cli_command", "profile set-key") == "intentionally_cli_only"
    assert _matrix_status("cli_command", "forge exec") == "blocked_until_security_model"
    assert _matrix_status("cli_command", "forge swarm") == "implemented_in_extension"
    assert _matrix_status("cli_command", "hooks watch") == "blocked_until_security_model"
    assert _matrix_status("cli_command", "mcp auth login") == "implemented_in_extension"
    assert _matrix_status("cli_forge_chat_command", "/execute") == "experimental_with_gate"


def test_high_risk_features_are_not_left_planned() -> None:
    for surface, name in (
        ("cli_command", "forge exec"),
        ("cli_command", "hooks watch"),
        ("ide_capability", "features.forge.execute.unsafe_modes"),
    ):
        assert _matrix_status(surface, name) == "blocked_until_security_model"
    # forge swarm graduated all the way to a first-class IDE workflow; it must
    # never drift into an ambiguous planned_for_protocol status.
    assert _matrix_status("cli_command", "forge swarm") == "implemented_in_extension"
    assert _matrix_status("cli_command", "mcp auth login") == "implemented_in_extension"
    assert _matrix_status("ide_bridge_method", "mcp.auth.login.start") == (
        "implemented_in_extension"
    )


def test_forge_cancel_is_classified_as_cooperative_extension_capability() -> None:
    assert _matrix_status("ide_bridge_method", "forge.cancel") == "implemented_in_extension"
    entry = _matrix_entry("ide_bridge_method", "forge.cancel")
    assert "cooperative checkpoint cancellation" in entry["rationale"]
    assert "not a hard interrupt" in entry["security_or_lifecycle_reason"]
