from __future__ import annotations

import hashlib
import json

import pytest

from alysis_code.extensions.models import RegistryEntry, RegistryFile
from alysis_code.extensions.workspace_trust import grant_workspace_trust
from alysis_code.ide.management_protocol import ProtocolError, handle_management_method


@pytest.mark.parametrize("params", [{}, {"query": ""}, {"query": " \t "}])
def test_management_search_lists_full_registry_without_query(monkeypatch, params):
    registry = RegistryFile(
        extensions=[
            RegistryEntry(
                id=f"acme.{name}",
                name=name,
                description=f"{name} integration",
                repo=f"https://github.com/acme/{name}",
                commit="a" * 40,
            )
            for name in ("github", "notes")
        ]
    )
    monkeypatch.setattr("alysis_code.ide.management_protocol.load_registry", lambda: registry)
    result = handle_management_method("ext.search", params, request_id=1)
    assert result["count"] == 2
    assert [entry["id"] for entry in result["extensions"]] == ["acme.github", "acme.notes"]
    filtered = handle_management_method("ext.search", {"query": " NOTES "}, request_id=2)
    assert filtered["count"] == 1
    assert filtered["extensions"][0]["id"] == "acme.notes"


@pytest.mark.parametrize("query", [None, True, 7, [], {}])
def test_management_search_rejects_non_string_query(query):
    with pytest.raises(ProtocolError, match="query must be a string"):
        handle_management_method("ext.search", {"query": query}, request_id=1)


def test_management_enablement_matches_runtime_workspace_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    state_path = tmp_path / "data" / "extensions" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"installed": {"acme.demo": {"enabled": False}}}))
    project = tmp_path / "project"
    overrides = project / ".alysis" / "extensions.json"
    overrides.parent.mkdir(parents=True)
    overrides.write_text(json.dumps({"enabled": ["acme.demo"]}))

    def status():
        listing = handle_management_method("ext.list", {"workspace": str(project)}, request_id=1)
        info = handle_management_method(
            "ext.info", {"workspace": str(project), "plugin_id": "acme.demo"}, request_id=2
        )
        return listing, info

    listing, info = status()
    assert not listing["extensions"][0]["enabled_effective"]
    assert not info["enabled_effective"]
    assert listing["workspace_trust"]["state"] == "untrusted"
    grant_workspace_trust(
        repo_root=project, overrides_sha256=hashlib.sha256(overrides.read_bytes()).hexdigest()
    )
    listing, info = status()
    assert listing["extensions"][0]["enabled_effective"]
    assert info["enabled_effective"]
    assert listing["workspace_trust"]["state"] == "trusted"
    overrides.write_text(overrides.read_text() + "\n")
    listing, info = status()
    assert not listing["extensions"][0]["enabled_effective"]
    assert not info["enabled_effective"]
