"""Catalog details the Marketplace screen shows for the built-in plugins."""

from __future__ import annotations

import tomllib

from alysis_code.extensions.bundled import bundled_manifest
from alysis_code.extensions.registry import find_by_id, load_registry
from alysis_code.ide.management_protocol import handle_management_method

CATEGORIES = {"code", "docs-files", "mail-chat", "calendar", "notes-wiki", "tasks-projects"}


def test_every_built_in_plugin_has_display_details():
    result = handle_management_method("ext.search", {}, request_id=1)
    assert result["count"] == 19
    for entry in result["extensions"]:
        assert entry["categories"], entry["id"]
        assert set(entry["categories"]) <= CATEGORIES, entry["id"]
        assert entry["publisher"] == "Alysis"
        assert entry["tier"] == "alysis"
        assert entry["catalog_source"] == "built-in"


def test_connects_to_is_read_from_the_pinned_descriptor():
    result = handle_management_method("ext.search", {}, request_id=1)
    registry = load_registry()
    for entry in result["extensions"]:
        descriptor = tomllib.loads(
            bundled_manifest(find_by_id(registry, entry["id"])).decode("utf-8")
        )
        components = descriptor["components"]
        urls = [server["url"] for server in components.get("mcp_server", [])] + [
            origin
            for connector in components.get("verified_connector", [])
            for origin in (connector["url"], *connector.get("oauth_sign_in_origins", []))
        ]
        assert entry["connects_to"] == urls, entry["id"]
    gmail = next(e for e in result["extensions"] if e["id"] == "alysis.google_gmail")
    assert gmail["connects_to"] == ["https://gmailmcp.googleapis.com/mcp/v1"]
    notion = next(e for e in result["extensions"] if e["id"] == "alysis.notion")
    assert notion["connects_to"] == ["https://mcp.notion.com/mcp"]


def test_a_descriptor_that_fails_its_pin_shows_no_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    registry = load_registry()
    entry = find_by_id(registry, "alysis.github")
    entry.package_sha256 = "0" * 64
    monkeypatch.setattr("alysis_code.ide.management_protocol.load_registry", lambda: registry)
    result = handle_management_method(
        "ext.info", {"workspace": str(tmp_path), "plugin_id": "alysis.github"}, request_id=1
    )
    assert result["extension"]["connects_to"] == []
    assert result["needs_review"] is False
