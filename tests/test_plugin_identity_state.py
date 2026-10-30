"""An unreadable identity namespace cannot be treated as an empty namespace."""

from __future__ import annotations

import pytest
from test_plugin_install import _env, _mock_clone

from alysis_code.extensions import install as installer
from alysis_code.extensions.state import load_global_state


@pytest.mark.parametrize("contents", ["{broken fixture", "[]", "null"])
def test_user_install_refuses_malformed_project_identity_state(tmp_path, monkeypatch, contents):
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    config = tmp_path / ".alysis/extensions.json"
    config.parent.mkdir()
    config.write_text(contents)
    approvals = []
    with pytest.raises(RuntimeError, match="Invalid JSON"):
        installer.install_plugin(
            source="git+https://example.invalid/plugin.git@" + "a" * 40,
            repo_root=tmp_path,
            trust_prompt=lambda request: approvals.append(request) or True,
        )
    assert approvals == []
    assert load_global_state().installed == {}
    assert config.read_text() == contents
    assert not (tmp_path / "data/extensions/installed").exists()


def test_user_install_refuses_unreadable_project_identity_state(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)

    def unreadable(root):
        raise RuntimeError("Failed to read synthetic project identity state")

    monkeypatch.setattr(installer, "load_project_state", unreadable)
    with pytest.raises(RuntimeError, match="Failed to read"):
        installer.install_plugin(
            source="git+https://example.invalid/plugin.git@" + "a" * 40,
            repo_root=tmp_path,
            trust_prompt=lambda request: pytest.fail("No consent on unknown identity"),
        )
    assert load_global_state().installed == {}


def test_genuinely_absent_project_identity_state_still_allows_install(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    result = installer.install_plugin(
        source="git+https://example.invalid/plugin.git@" + "a" * 40,
        repo_root=tmp_path,
        trust_prompt=lambda request: True,
    )
    assert result.plugin_id == "acme.demo"
    assert set(load_global_state().installed) == {"acme.demo"}
