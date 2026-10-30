"""Manifest suffixes cannot introduce shell syntax into installed hook names."""

from __future__ import annotations

import json

import pytest

from alysis_code.extensions import install as install_mod
from alysis_code.extensions.manifest import load_manifest
from tests.test_plugin_install import _manifest


def install_hooks(tmp_path, monkeypatch, platform, suffix):
    staging = tmp_path / "staging"
    staging.mkdir()
    # Put a valid hook first: rejection must happen before copying that hook too.
    text = _manifest(components=False)
    for index, name in enumerate(["first.sh", "start" + suffix]):
        (staging / name).write_text("# inert; never executed\n")
        text += (
            '\n[[components.hook]]\nevent = "SessionStart"\n'
            f'path = {json.dumps(name)}\nid = "hook{index}"\n'
        )
    (staging / "alysis-plugin.toml").write_text(text)
    manifest = load_manifest(staging)
    with monkeypatch.context() as patch:
        patch.setattr(install_mod.sys, "platform", platform)
        patch.setattr(install_mod.sys, "executable", "C:/Python Directory/python.exe")
        install_mod._install_hooks(
            manifest=manifest,
            staging_root=staging,
            repo_root=tmp_path,
            project=False,
            user_config_dir=tmp_path / "config",
            rollback=[],
        )


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize(
    "suffix",
    [
        ".bat&calc",
        ".bat|calc",
        ".cmd%PATH%",
        ".cmd^x",
        ".cmd!VAR!",
        ".sh;echo",
        ".sh\nnext",
        ".é",
        "." + "a" * 17,
    ],
)
def test_unsafe_suffix_refused_before_any_hook_copy(tmp_path, monkeypatch, platform, suffix):
    with pytest.raises(install_mod.PluginInstallError, match="hook file suffix"):
        install_hooks(tmp_path, monkeypatch, platform, suffix)
    assert not (tmp_path / "config" / "hooks.json").exists()
    assert not (tmp_path / "config" / "hooks").exists()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("suffix", ["", ".py", ".sh", ".cmd", ".bat", ".ps1", ".A123"])
def test_safe_suffix_persists_platform_command(tmp_path, monkeypatch, platform, suffix):
    install_hooks(tmp_path, monkeypatch, platform, suffix)
    config = tmp_path / "config"
    data = json.loads((config / "hooks.json").read_text())
    command = data["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    path = config / "hooks/plugins/acme-demo" / ("hook1" + suffix)
    assert path.read_text() == "# inert; never executed\n"
    with monkeypatch.context() as patch:
        patch.setattr(install_mod.sys, "platform", platform)
        patch.setattr(install_mod.sys, "executable", "C:/Python Directory/python.exe")
        assert command == install_mod._hook_command(path)
    if platform == "win32":
        assert not any(c in command for c in "&|%^!\n")
