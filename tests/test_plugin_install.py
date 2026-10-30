from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from alysis_code.extensions import install as install_mod
from alysis_code.extensions.install import (
    ComponentInstallSummary,
    PluginInstallError,
    install_plugin,
    uninstall_plugin,
)
from alysis_code.extensions.models import ExtensionState, InstalledExtensionState
from alysis_code.extensions.registry import RegistryEntry, RegistryFile
from alysis_code.extensions.state import load_global_state
from alysis_code.skills.install import SkillInstallResult

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.skills.paths.canonical_user_config_dir", lambda: tmp_path / "config"
    )
    monkeypatch.setattr(
        "alysis_code.skills.discovery.canonical_user_config_dir", lambda: tmp_path / "config"
    )


def _manifest(
    *,
    plugin_id: str = "acme.demo",
    alysis: str = ">=0.1,<1",
    platforms: str = '["linux", "darwin", "windows"]',
    components: bool = True,
) -> str:
    text = f"""schema_version = 1

[plugin]
id = "{plugin_id}"
name = "Demo Plugin"
version = "1.2.3"
description = "Demo plugin for tests"
author = "Acme"
license = "MIT"

[compatibility]
alysis = "{alysis}"
platforms = {platforms}
"""
    if not components:
        return text
    return (
        text
        + """
[[components.skill]]
path = "skill"

[[components.tool]]
path = "tools/demo.py"
id = "demo_tool"
description = "Demo custom tool"
required_env = ["TOKEN"]
network = true
filesystem = "write"
timeout_sec = 30

[[components.mcp_server]]
id = "demo_server"
transport = "stdio"
command = ["python", "server.py"]
env = ["MCP_TOKEN"]
scopes = ["tools"]

[[components.hook]]
event = "SessionStart"
path = "hooks/start.py"
id = "start"
"""
    )


def _write_plugin_tree(
    root: Path,
    *,
    plugin_id: str = "acme.demo",
    alysis: str = ">=0.1,<1",
    platforms: str = '["linux", "darwin", "windows"]',
    components: bool = True,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alysis-plugin.toml").write_text(
        _manifest(
            plugin_id=plugin_id,
            alysis=alysis,
            platforms=platforms,
            components=components,
        ),
        encoding="utf-8",
    )
    if not components:
        return
    (root / "skill").mkdir()
    (root / "skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    (root / "tools" / "demo.py").write_text(
        "TOOL = {\n"
        "    'name': 'demo_tool',\n"
        "    'description': 'Demo custom tool',\n"
        "    'input_schema': {'type': 'object', 'properties': {}},\n"
        "}\n\n"
        "def run(args):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (root / "server.py").write_text("print('server')\n", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "start.py").write_text("print('hook')\n", encoding="utf-8")


def _mock_clone(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    def clone(*, git_url: str, commit: str, destination: Path) -> None:
        _write_plugin_tree(destination, **kwargs)

    monkeypatch.setattr(install_mod, "_clone_pinned_git_repo", clone)


def _mock_registry(monkeypatch: pytest.MonkeyPatch, *, commit: str = COMMIT_A) -> None:
    monkeypatch.setattr(
        install_mod,
        "load_registry",
        lambda: RegistryFile(
            extensions=[
                RegistryEntry(
                    id="acme.demo",
                    name="Demo",
                    description="Demo",
                    repo="https://example.com/acme/demo.git",
                    commit=commit,
                )
            ]
        ),
    )


def _mock_skill_installer(monkeypatch: pytest.MonkeyPatch, calls: list[Path] | None = None) -> None:
    def install_skill_bundle(**kwargs: object) -> SkillInstallResult:
        if calls is not None:
            calls.append(Path(str(kwargs["source"])))
        return SkillInstallResult(
            bundle_path=Path(str(kwargs["workspace_root"])) / "installed-skill",
            installed_name="demo-skill",
            source_kind="dir",
        )

    monkeypatch.setattr(install_mod, "install_skill_bundle", install_skill_bundle)


def test_happy_path_registry_id_installs_state_and_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    _mock_registry(monkeypatch)
    skill_calls: list[Path] = []
    _mock_skill_installer(monkeypatch, skill_calls)

    result = install_plugin(
        source="acme.demo",
        repo_root=tmp_path / "repo",
        trust_prompt=lambda request: True,
    )

    assert result.plugin_id == "acme.demo"
    assert result.components_installed.skill_ids == ("demo-skill",)
    assert result.components_installed.tool_ids == ("demo_tool",)
    assert result.components_installed.mcp_server_ids == ("demo_server",)
    assert result.components_installed.hook_ids == ("start",)
    assert skill_calls and skill_calls[0].name == "skill"
    state = load_global_state()
    record = state.installed["acme.demo"]
    assert record.enabled is True
    assert "acme.demo" in state.enabled
    assert record.manifest_sha256 == result.manifest_sha256
    assert record.component_ids["tool"] == ["demo_tool"]
    assert (tmp_path / "config" / "tools" / "plugins" / "acme-demo" / "demo_tool.py").exists()
    assert "acme.demo/demo_server" in (tmp_path / "config" / "mcp.json").read_text()
    assert "acme-demo.start" in (tmp_path / "config" / "hooks.json").read_text()


def test_happy_path_direct_git_url_at_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)

    result = install_plugin(
        source=f"git+https://example.com/acme/demo.git@{COMMIT_A}",
        repo_root=tmp_path,
        trust_prompt=lambda request: True,
    )

    assert result.commit == COMMIT_A
    assert result.components_installed == ComponentInstallSummary((), (), (), ())


def test_happy_path_direct_git_url_fragment_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)

    result = install_plugin(
        source=f"https://example.com/acme/demo.git#{COMMIT_A}",
        repo_root=tmp_path,
        trust_prompt=lambda request: True,
    )

    assert result.commit == COMMIT_A


def test_idempotency_same_commit_and_manifest_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    _mock_registry(monkeypatch)
    prompts: list[bool] = []

    install_plugin(
        source="acme.demo",
        repo_root=tmp_path,
        trust_prompt=lambda request: prompts.append(True) or True,
    )
    result = install_plugin(
        source="acme.demo",
        repo_root=tmp_path,
        trust_prompt=lambda request: prompts.append(True) or True,
    )

    assert prompts == [True]
    assert result.trust_was_prompted is False


def test_reinstall_new_commit_sets_prompt_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    _mock_registry(monkeypatch, commit=COMMIT_A)
    install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)
    _mock_registry(monkeypatch, commit=COMMIT_B)
    flags: list[bool] = []

    install_plugin(
        source="acme.demo",
        repo_root=tmp_path,
        trust_prompt=lambda request: flags.append(request.is_reinstall_with_new_commit) or True,
    )

    assert flags == [True]


def test_trust_prompt_false_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    _mock_registry(monkeypatch)
    _mock_skill_installer(monkeypatch)

    with pytest.raises(PluginInstallError, match="install rejected"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: False)

    assert not (tmp_path / "data" / "extensions" / "state.json").exists()
    assert not (tmp_path / "config" / "tools").exists()


def test_partial_install_failure_rolls_back_earlier_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    _mock_registry(monkeypatch)
    removed: list[str] = []
    _mock_skill_installer(monkeypatch)
    monkeypatch.setattr(
        install_mod,
        "remove_managed_skill",
        lambda **kwargs: removed.append(str(kwargs["name"])),
    )
    monkeypatch.setattr(
        install_mod,
        "_install_tools",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("tool boom")),
    )

    with pytest.raises(PluginInstallError, match="tool boom"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)

    assert removed == ["demo-skill"]
    assert not (tmp_path / "data" / "extensions" / "state.json").exists()


def test_manifest_validation_failure_aborts_before_subsystems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_registry(monkeypatch)

    def clone(*, git_url: str, commit: str, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "alysis-plugin.toml").write_text("schema_version = 1\n", encoding="utf-8")

    monkeypatch.setattr(install_mod, "_clone_pinned_git_repo", clone)
    monkeypatch.setattr(
        install_mod,
        "install_skill_bundle",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not install")),
    )

    with pytest.raises(PluginInstallError, match="manifest validation failed"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)


def test_compatibility_version_mismatch_mentions_running_and_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_registry(monkeypatch)
    _mock_clone(monkeypatch, components=False, alysis=">=999")

    with pytest.raises(PluginInstallError) as excinfo:
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)

    assert "running" in str(excinfo.value)
    assert ">=999" in str(excinfo.value)


def test_platform_mismatch_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    _mock_registry(monkeypatch)
    _mock_clone(monkeypatch, components=False, platforms='["linux"]')
    monkeypatch.setattr(install_mod, "_current_platform", lambda: "windows")

    with pytest.raises(PluginInstallError, match="platform"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)


@pytest.mark.parametrize("plugin_id", ["alysis.github", "alysis_.github", "alysis.google-drive"])
def test_git_source_cannot_claim_the_first_party_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_id: str
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, plugin_id=plugin_id, components=False)

    with pytest.raises(PluginInstallError, match="reserved for plugins bundled"):
        install_plugin(
            source=f"git+https://example.com/acme/demo.git@{COMMIT_A}",
            repo_root=tmp_path,
            trust_prompt=lambda request: pytest.fail("A reserved id must fail before trust review"),
        )
    assert load_global_state().installed == {}


def test_plugin_ids_that_share_an_install_folder_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, plugin_id="acme.demo", components=False)
    install_plugin(
        source=f"git+https://example.com/acme/demo.git@{COMMIT_A}",
        repo_root=tmp_path,
        trust_prompt=lambda request: True,
    )
    _mock_clone(monkeypatch, plugin_id="acme_.demo", components=False)

    with pytest.raises(PluginInstallError, match="collides with acme.demo"):
        install_plugin(
            source=f"git+https://example.com/other/demo.git@{COMMIT_B}",
            repo_root=tmp_path,
            trust_prompt=lambda request: pytest.fail(
                "A colliding id must fail before trust review"
            ),
        )
    assert set(load_global_state().installed) == {"acme.demo"}


def test_catalog_entry_must_contain_its_own_plugin_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, plugin_id="acme.other", components=False)
    _mock_registry(monkeypatch)

    with pytest.raises(PluginInstallError, match="contains a different plugin id"):
        install_plugin(
            source="acme.demo",
            repo_root=tmp_path,
            trust_prompt=lambda request: pytest.fail("A mismatched catalog entry must fail first"),
        )


def test_registry_id_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(install_mod, "load_registry", lambda: RegistryFile(extensions=[]))

    with pytest.raises(PluginInstallError, match="registry id not found"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/acme/demo.git",
        f"git+https://example.com/acme/demo.git@{'main'}",
        "not a source",
    ],
)
def test_malformed_source_string_rejected(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)

    with pytest.raises(PluginInstallError, match="unsupported install source"):
        install_plugin(source=source, repo_root=tmp_path, trust_prompt=lambda request: True)


def test_commit_mismatch_after_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "0" * 40 if args[-2:] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

    with pytest.raises(PluginInstallError, match="checkout mismatch"):
        install_plugin(
            source=f"git+https://example.com/acme/demo.git@{COMMIT_A}",
            repo_root=tmp_path,
            trust_prompt=lambda request: True,
        )


def test_git_timeout_produces_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)

    def timeout_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(install_mod.subprocess, "run", timeout_run)

    with pytest.raises(PluginInstallError, match="timed out"):
        install_plugin(
            source=f"git+https://example.com/acme/demo.git@{COMMIT_A}",
            repo_root=tmp_path,
            trust_prompt=lambda request: True,
        )


def test_uninstall_removes_components_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch)
    _mock_registry(monkeypatch)
    _mock_skill_installer(monkeypatch)
    removed_skills: list[str] = []
    monkeypatch.setattr(
        install_mod,
        "remove_managed_skill",
        lambda **kwargs: removed_skills.append(str(kwargs["name"])),
    )
    install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)

    result = uninstall_plugin(plugin_id="acme.demo", repo_root=tmp_path)

    assert result.components_removed.skill_ids == ("demo-skill",)
    assert removed_skills == ["demo-skill"]
    assert load_global_state().installed == {}


def test_uninstall_when_not_installed_in_requested_scope(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="plugin not installed in project"):
        uninstall_plugin(plugin_id="acme.demo", repo_root=tmp_path, project=True)


def test_uninstall_best_effort_continues_after_component_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    state = ExtensionState(
        installed={
            "acme.demo": InstalledExtensionState(
                id="acme.demo",
                component_ids={
                    "hook": ["start"],
                    "mcp_server": ["server"],
                    "tool": ["tool"],
                    "skill": ["skill"],
                },
            )
        }
    )
    (tmp_path / "data" / "extensions").mkdir(parents=True)
    (tmp_path / "data" / "extensions" / "state.json").write_text(
        json.dumps(state.model_dump(mode="json")),
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        install_mod,
        "_remove_hook_component",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("hook failed")),
    )
    monkeypatch.setattr(
        install_mod, "_remove_mcp_server_component", lambda **kwargs: calls.append("mcp")
    )
    monkeypatch.setattr(
        install_mod, "_remove_tool_component", lambda **kwargs: calls.append("tool")
    )
    monkeypatch.setattr(install_mod, "remove_managed_skill", lambda **kwargs: calls.append("skill"))

    with pytest.raises(PluginInstallError) as excinfo:
        uninstall_plugin(plugin_id="acme.demo", repo_root=tmp_path)

    assert "uninstall completed with errors" in str(excinfo.value)
    assert calls == ["mcp", "tool", "skill"]
    assert load_global_state().installed == {}


def test_installed_at_is_parseable_iso_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    _mock_registry(monkeypatch)

    install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)

    installed_at = load_global_state().installed["acme.demo"].installed_at
    assert installed_at is not None
    assert datetime.fromisoformat(installed_at).tzinfo is not None


def test_state_round_trip_preserves_new_fields() -> None:
    state = ExtensionState(
        installed={
            "acme.demo": InstalledExtensionState(
                id="acme.demo",
                manifest_sha256="abc",
                installed_at="2026-05-01T00:00:00+00:00",
                source_url="https://example.com/repo.git",
                scope="user",
                component_ids={"tool": ["demo"]},
            )
        }
    )

    parsed = ExtensionState.model_validate(state.model_dump(mode="json"))

    record = parsed.installed["acme.demo"]
    assert record.manifest_sha256 == "abc"
    assert record.scope == "user"
    assert record.component_ids == {"tool": ["demo"]}


def test_atomicity_write_failure_leaves_previous_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _mock_clone(monkeypatch, components=False)
    _mock_registry(monkeypatch)
    state_path = tmp_path / "data" / "extensions" / "state.json"
    state_path.parent.mkdir(parents=True)
    original = {"schema_version": 1, "installed": {"old.ext": {"id": "old.ext"}}}
    state_path.write_text(json.dumps(original), encoding="utf-8")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("write failed")

    monkeypatch.setattr("alysis_code.extensions.state.atomic_write_json", fail_write)

    with pytest.raises(PluginInstallError, match="write failed"):
        install_plugin(source="acme.demo", repo_root=tmp_path, trust_prompt=lambda request: True)

    assert json.loads(state_path.read_text(encoding="utf-8")) == original
