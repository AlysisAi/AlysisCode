"""Project plugin installation preserves MCP scope and approval boundaries."""

import hashlib
import json

import pytest

from alysis_code.extensions import install as installer
from tests.test_plugin_install import _env, _manifest, _mock_clone


@pytest.mark.parametrize("existing", [False, True])
def test_project_mcp_refusal_precedes_review_noop_and_live_mutation(
    tmp_path, monkeypatch, existing
):
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    if existing:
        state = tmp_path / ".alysis/extensions.json"
        state.parent.mkdir()
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "installed": {
                        "acme.demo": {
                            "id": "acme.demo",
                            "scope": "project",
                            "commit": "a" * 40,
                            "manifest_sha256": hashlib.sha256(_manifest().encode()).hexdigest(),
                            "source_kind": "git",
                            "enabled": True,
                        }
                    },
                    "enabled": [],
                }
            )
        )
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    for name in ("_load_extension_state", "_install_plugin_tree", "_install_mcp_servers"):
        monkeypatch.setattr(installer, name, lambda *a, **k: pytest.fail("Past preflight"))
    with pytest.raises(
        installer.PluginInstallError, match="^project plugin MCP definitions are not supported$"
    ):
        installer.install_plugin(
            source="git+https://example.invalid/p.git@" + "a" * 40,
            repo_root=tmp_path,
            project=True,
            trust_prompt=lambda _: pytest.fail("Project MCP reached review"),
        )
    assert before == {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


def test_user_direct_install_retains_existing_behavior(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    prompts = []
    result = installer.install_plugin(
        source="git+https://example.invalid/p.git@" + "a" * 40,
        repo_root=tmp_path,
        project=False,
        trust_prompt=lambda request: prompts.append(request) or True,
    )
    assert len(prompts) == 1
    assert result.components_installed.mcp_server_ids == ("demo_server",)
    config = json.loads((tmp_path / "config/mcp.json").read_text())
    assert any(row.get("command") == "python" for row in config["servers"].values())


@pytest.mark.parametrize("plugin_id", ["alysis.github", "alysis.google_drive"])
def test_project_bundled_host_declaration_does_not_write_user_mcp(tmp_path, monkeypatch, plugin_id):
    _env(monkeypatch, tmp_path)
    config_path = tmp_path / "config/mcp.json"
    config_path.parent.mkdir()
    original = b'{"schema_version":1,"servers":{}}\n'
    config_path.write_bytes(original)
    monkeypatch.setattr(installer, "_clone_pinned_git_repo", lambda **k: pytest.fail("No clone"))
    result = installer.install_plugin(
        source=plugin_id,
        repo_root=tmp_path,
        project=True,
        trust_prompt=lambda _: True,
    )
    assert result.plugin_id == plugin_id
    assert result.components_installed.mcp_server_ids
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("host,enabled", [(True, True), (False, False)])
def test_project_host_impostor_refuses_but_disabled_direct_preserved(
    tmp_path, monkeypatch, host, enabled
):
    _env(monkeypatch, tmp_path)
    content = (
        _manifest(components=False)
        + f"""
[[components.mcp_server]]
id = "fixture"
transport = "http"
url = "https://example.invalid/mcp"
scopes = ["tools"]
host_managed = {str(host).lower()}
enabled = {str(enabled).lower()}
"""
    )

    def clone(*, destination, **kwargs):
        (destination / "alysis-plugin.toml").write_text(content)

    monkeypatch.setattr(installer, "_clone_pinned_git_repo", clone)
    args = dict(
        source="git+https://example.invalid/p.git@" + "a" * 40,
        repo_root=tmp_path,
        project=True,
        trust_prompt=lambda _: True,
    )
    if host:
        args["trust_prompt"] = lambda _: pytest.fail("Host impostor reached review")
        with pytest.raises(
            installer.PluginInstallError, match="project plugin MCP definitions are not supported"
        ):
            installer.install_plugin(**args)
    else:
        assert installer.install_plugin(**args).components_installed.mcp_server_ids == ()
    assert not (tmp_path / "config/mcp.json").exists()
