"""Plugin consent never grants whole project-hook-config trust."""

from __future__ import annotations

import json

import pytest

from alysis_code.extensions import install as mod
from alysis_code.hooks import trust
from tests.test_plugin_install import _env, _manifest


@pytest.fixture
def hooks(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    def clone(*, destination, **_):
        (destination / "alysis-plugin.toml").write_text(
            _manifest(components=False)
            + '\n[[components.hook]]\nevent="SessionStart"\npath="start.py"\nid="start"\n'
        )
        (destination / "start.py").write_text("# never execute\n")

    monkeypatch.setattr(mod, "_clone_pinned_git_repo", clone)

    def install(commit="a"):
        return mod.install_plugin(
            source="git+https://fixture.invalid/p.git@" + commit * 40,
            repo_root=repo,
            project=True,
            trust_prompt=lambda _: True,
        )

    path = repo / ".alysis/hooks.json"
    path.parent.mkdir()
    unrelated = dict(type="command", id="unrelated", command="never-run-fixture")
    path.write_text(
        json.dumps(dict(schema_version=1, hooks=dict(SessionStart=[dict(hooks=[unrelated])])))
    )
    return repo, path, unrelated, install


@pytest.mark.parametrize("operation", ["install", "update", "uninstall", "rollback"])
@pytest.mark.parametrize("previously_trusted", [False, True])
def test_plugin_mutation_preserves_trust_bytes(hooks, monkeypatch, operation, previously_trusted):
    repo, path, unrelated, install = hooks
    if operation in {"update", "uninstall"}:
        install()
    # Also preserve another workspace's independent grant, byte for byte.
    other = repo.parent / "other"
    other.mkdir()
    other_config = other / "hooks.json"
    other_config.write_text("{}")
    trust.trust_project_hooks_config(workspace_root=other, config_path=other_config)
    if previously_trusted:
        trust.trust_project_hooks_config(workspace_root=repo, config_path=path)
    before = trust.trust_state_path().read_bytes()
    hooks_before = path.read_bytes()
    if operation == "rollback":
        monkeypatch.setattr(
            mod,
            "_save_extension_state",
            lambda **_: (_ for _ in ()).throw(OSError("inert write failure")),
        )
        with pytest.raises(mod.PluginInstallError, match="inert write failure"):
            install()
        assert path.read_bytes() == hooks_before
    elif operation == "uninstall":
        mod.uninstall_plugin(plugin_id="acme.demo", repo_root=repo, project=True)
    else:
        install("b" if operation == "update" else "a")
    assert trust.trust_state_path().read_bytes() == before
    raw = json.loads(path.read_text())
    assert unrelated in [
        h for groups in raw["hooks"].values() for group in groups for h in group["hooks"]
    ]
    if operation != "rollback":
        assert not trust.is_project_hooks_config_trusted(workspace_root=repo, config_path=path)
        # Separate explicit review still works for the new exact config bytes.
        trust.trust_project_hooks_config(workspace_root=repo, config_path=path)
        assert trust.is_project_hooks_config_trusted(workspace_root=repo, config_path=path)


def test_rollback_does_not_write_an_unchanged_trust_store(hooks, monkeypatch):
    _, _, _, install = hooks
    monkeypatch.setattr(
        mod,
        "save_hooks_trust_state",
        lambda *_a, **_k: pytest.fail("installer must not write whole-hook trust"),
    )
    monkeypatch.setattr(
        mod,
        "_save_extension_state",
        lambda **_: (_ for _ in ()).throw(OSError("inert state failure")),
    )
    with pytest.raises(mod.PluginInstallError, match="inert state failure") as caught:
        install()
    assert caught.value.rollback_errors == ()
