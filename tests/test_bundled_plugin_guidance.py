"""Shipped guidance must load through real pin/install/binding checks, not fixture text."""

import tomllib

import pytest

from alysis_code.extensions.bundled import bundled_manifest
from alysis_code.extensions.install import disable_plugin, install_plugin
from alysis_code.extensions.manifest import PluginManifest
from alysis_code.extensions.plugin_skills import active_plugin_skills, read_plugin_skill
from alysis_code.extensions.registry import load_registry

ENTRIES = [e for e in load_registry().extensions if e.source_kind == "bundled"]


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e.id)
def test_bundled_guidance_is_pinned_bound_readable_and_revocable(entry, tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git",
        lambda *a, **k: pytest.fail("Bundled install must be offline"),
    )
    manifest = PluginManifest.model_validate(tomllib.loads(bundled_manifest(entry).decode()))
    assert manifest.components.plugin_skill, entry.id
    if manifest.components.verified_connector:
        tools = {tool.name for c in manifest.components.verified_connector for tool in c.tools}
        for skill in manifest.components.plugin_skill:
            assert set(skill.uses_tools) <= tools
    root = tmp_path / "workspace"
    root.mkdir()
    install_plugin(source=entry.id, repo_root=root, trust_prompt=lambda _: True)
    assert active_plugin_skills(root) == ()
    if entry.id == "alysis.github":
        binding = {"host_mcp": {}}
    elif entry.id.startswith("alysis.google_"):
        binding = {"google_mcp": [{"service": entry.id.removeprefix("alysis.google_")}]}
    else:
        binding = {"connector_mcp": [{"plugin_id": entry.id}]}
    skills = active_plugin_skills(root, **binding)
    assert len(skills) == len(manifest.components.plugin_skill)
    for skill in skills:
        result = read_plugin_skill(root, skill)
        assert result["trust_level"] == "publisher_guidance"
        assert skill.text in result["content"]
    disable_plugin(plugin_id=entry.id, repo_root=root)
    assert active_plugin_skills(root, **binding) == ()
    assert all("error" in read_plugin_skill(root, skill) for skill in skills)
