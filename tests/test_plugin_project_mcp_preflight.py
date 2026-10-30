"""Unwired P4 preflight: project definitions cannot become user MCP entries."""

from dataclasses import replace

import pytest

from alysis_code.extensions import install as installer
from alysis_code.extensions.bundled import parse_bundled_manifest
from alysis_code.extensions.manifest import McpServerComponent
from tests.test_plugin_install import _manifest


def manifest(transport="stdio", *, enabled=True, host=False):
    parsed = parse_bundled_manifest(_manifest(components=False).encode())
    fields = (
        {"command": ["python", "fixture.py"]}
        if transport == "stdio"
        else {
            "url": "https://example.invalid/mcp",
        }
    )
    server = McpServerComponent(
        id="fixture",
        transport=transport,
        scopes=["tools"],
        enabled=enabled,
        host_managed=host,
        **fields,
    )
    return parsed.model_copy(
        update={
            "components": parsed.components.model_copy(
                update={"mcp_server": [server]},
            )
        }
    )


def git_source():
    return installer._resolve_source("git+https://example.invalid/p.git@" + "a" * 40)


def test_user_scope_returns_before_inspecting_manifest_or_source():
    installer._check_project_mcp_scope(object(), object(), project=False)


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_enabled_project_direct_refused_with_fixed_error(transport):
    with pytest.raises(
        installer.PluginInstallError, match="^project plugin MCP definitions are not supported$"
    ):
        installer._check_project_mcp_scope(manifest(transport), git_source(), project=True)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("project,enabled", [(False, True), (False, False), (True, False)])
def test_user_and_disabled_direct_components_preserved(transport, project, enabled):
    installer._check_project_mcp_scope(
        manifest(transport, enabled=enabled), git_source(), project=project
    )


def test_metadata_and_other_project_components_preserved():
    parsed = parse_bundled_manifest(_manifest(components=False).encode())
    installer._check_project_mcp_scope(parsed, git_source(), project=True)
    parsed = parse_bundled_manifest(_manifest().encode())
    parsed = parsed.model_copy(
        update={"components": parsed.components.model_copy(update={"mcp_server": []})}
    )
    installer._check_project_mcp_scope(parsed, git_source(), project=True)


@pytest.mark.parametrize(
    "plugin_id", ["alysis.github", "alysis.google_drive", "alysis.google_docs"]
)
def test_real_bundled_host_declaration_preserved_without_registry_mutation(plugin_id):
    source = installer._resolve_source(plugin_id)
    parsed = parse_bundled_manifest(source.bundled_content)
    installer._check_project_mcp_scope(parsed, source, project=True)


def test_host_flag_alone_does_not_exempt_developer_source():
    with pytest.raises(
        installer.PluginInstallError, match="project plugin MCP definitions are not supported"
    ):
        installer._check_project_mcp_scope(manifest("http", host=True), git_source(), project=True)


@pytest.mark.parametrize(
    "change",
    [
        {"custom_id": "custom.fixture"},
        {"registry_id": None},
        {"registry_id": "missing.fixture"},
        {"git_url": "https://example.invalid/p.git"},
        {"commit": "a" * 40},
        {"package_sha256": "0" * 64},
        {"bundled_content": b"changed"},
    ],
)
def test_inconsistent_source_cannot_claim_host_exemption(change):
    source = installer._resolve_source("alysis.github")
    parsed = parse_bundled_manifest(source.bundled_content)
    with pytest.raises(
        installer.PluginInstallError, match="project plugin MCP definitions are not supported"
    ):
        installer._check_project_mcp_scope(parsed, replace(source, **change), project=True)


def test_changed_loaded_manifest_does_not_reuse_genuine_source_exemption():
    source = installer._resolve_source("alysis.github")
    parsed = parse_bundled_manifest(source.bundled_content)
    changed = parsed.components.mcp_server[0].model_copy(
        update={"url": "https://example.invalid/mcp"}
    )
    parsed = parsed.model_copy(
        update={"components": parsed.components.model_copy(update={"mcp_server": [changed]})}
    )
    with pytest.raises(
        installer.PluginInstallError, match="project plugin MCP definitions are not supported"
    ):
        installer._check_project_mcp_scope(parsed, source, project=True)


def test_unavailable_catalog_error_is_bounded(monkeypatch):
    source = installer._resolve_source("alysis.github")
    parsed = parse_bundled_manifest(source.bundled_content)

    def unavailable():
        raise OSError("private path sentinel")

    monkeypatch.setattr(installer, "load_registry", unavailable)
    with pytest.raises(installer.PluginInstallError) as caught:
        installer._check_project_mcp_scope(parsed, source, project=True)
    assert str(caught.value) == "project plugin MCP definitions are not supported"
