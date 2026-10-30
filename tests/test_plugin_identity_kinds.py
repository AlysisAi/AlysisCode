"""Source ownership checks through the installer, with inert source ports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from alysis_code.extensions import install as installer
from alysis_code.extensions.models import RegistryEntry, RegistryFile
from tests.test_custom_plugins import _descriptor
from tests.test_plugin_install import _env, _manifest, _mock_clone


def prepare(tmp_path, monkeypatch, kind):
    _env(monkeypatch, tmp_path)
    plugin_id = "custom.tools" if kind == "custom" else "acme.demo"
    content = (_descriptor() if kind == "custom" else _manifest(components=False)).encode()
    if kind == "git":
        _mock_clone(monkeypatch, components=False)
        source = "git+https://example.invalid/plugin.git@" + "a" * 40
    elif kind == "bundled":
        entry = RegistryEntry(
            id=plugin_id,
            name="Fixture",
            description="Fixture",
            repo="",
            commit="a" * 40,
            source_kind="bundled",
        )
        monkeypatch.setattr(installer, "load_registry", lambda: RegistryFile(extensions=[entry]))
        monkeypatch.setattr(installer, "bundled_manifest", lambda _: content)
        source = plugin_id
    else:
        source = "custom:" + plugin_id
    kwargs = {"source": source, "repo_root": tmp_path}
    if kind == "custom":
        kwargs["custom_descriptor"] = content
    resolved = installer._resolve_source(source, custom_descriptor=kwargs.get("custom_descriptor"))
    return plugin_id, content, resolved, kwargs


def seed(tmp_path, scope, plugin_id, content, resolved, kind):
    path = (
        tmp_path / "data/extensions/state.json"
        if scope == "user"
        else tmp_path / ".alysis/extensions.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": plugin_id,
        "scope": scope,
        "commit": resolved.commit,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "enabled": False,
    }
    if kind is not None:
        record["source_kind"] = kind
    path.write_text(
        json.dumps({"schema_version": 1, "installed": {plugin_id: record}, "enabled": []})
    )
    return path, path.read_bytes()


@pytest.mark.parametrize("scope", ["user", "project"])
@pytest.mark.parametrize(
    "incoming,stored",
    [(a, b) for a in ["git", "bundled", "custom"] for b in ["git", "bundled", "custom"] if a != b],
)
def test_same_id_different_kind_refuses_even_same_commit_before_consent(
    tmp_path, monkeypatch, scope, incoming, stored
):
    plugin_id, content, resolved, kwargs = prepare(tmp_path, monkeypatch, incoming)
    path, old = seed(tmp_path, scope, plugin_id, content, resolved, stored)
    with pytest.raises(
        installer.PluginInstallError, match="plugin source kind conflicts with installed identity"
    ):
        installer.install_plugin(
            **kwargs, trust_prompt=lambda _: pytest.fail("No prompt on conflict")
        )
    assert path.read_bytes() == old
    assert not (tmp_path / "data/extensions/installed").exists()


@pytest.mark.parametrize("kind", ["git", "bundled", "custom"])
def test_same_kind_noop_preserves_disabled_record(tmp_path, monkeypatch, kind):
    plugin_id, content, resolved, kwargs = prepare(tmp_path, monkeypatch, kind)
    path, old = seed(tmp_path, "user", plugin_id, content, resolved, kind)
    result = installer.install_plugin(
        **kwargs, trust_prompt=lambda _: pytest.fail("No prompt for noop")
    )
    assert result.source_kind == kind and not result.trust_was_prompted
    assert path.read_bytes() == old


@pytest.mark.parametrize("incoming", ["git", "bundled", "custom"])
def test_missing_persisted_kind_means_git(tmp_path, monkeypatch, incoming):
    plugin_id, content, resolved, kwargs = prepare(tmp_path, monkeypatch, incoming)
    path, old = seed(tmp_path, "user", plugin_id, content, resolved, None)
    if incoming == "git":
        assert not installer.install_plugin(
            **kwargs, trust_prompt=lambda _: False
        ).trust_was_prompted
    else:
        with pytest.raises(installer.PluginInstallError, match="source kind conflicts"):
            installer.install_plugin(**kwargs, trust_prompt=lambda _: pytest.fail("No consent"))
    assert path.read_bytes() == old


@pytest.mark.parametrize("scope", ["user", "project"])
def test_present_unknown_kind_is_not_treated_as_git(tmp_path, monkeypatch, scope):
    plugin_id, content, resolved, kwargs = prepare(tmp_path, monkeypatch, "git")
    path, old = seed(tmp_path, scope, plugin_id, content, resolved, "unknown")
    with pytest.raises(ValidationError):
        installer.install_plugin(**kwargs, trust_prompt=lambda _: pytest.fail("No consent"))
    assert path.read_bytes() == old


@pytest.mark.parametrize(
    "change",
    [
        {"registry_id": "custom.tools"},
        {"git_url": "https://example.invalid/wrong.git"},
        {"bundled_content": None},
    ],
)
def test_custom_source_coherence_preserves_descriptor_storage(tmp_path, monkeypatch, change):
    plugin_id, _, resolved, _ = prepare(tmp_path, monkeypatch, "custom")
    # A valid custom source necessarily carries bundled_content descriptor bytes.
    installer._check_plugin_identity(plugin_id, resolved, repo_root=tmp_path)
    with pytest.raises(
        installer.PluginInstallError, match="plugin source identity is inconsistent"
    ):
        installer._check_plugin_identity(plugin_id, replace(resolved, **change), repo_root=tmp_path)
